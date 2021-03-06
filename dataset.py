import os
import math
from ast import literal_eval
from collections import OrderedDict, defaultdict
import functools
import pickle
from torch.utils.data.distributed import DistributedSampler
import torch.distributed as dist
from pathos.helpers import mp
from pathos.multiprocessing import ProcessingPool as Pool
import zarr
import jsonlines
from tqdm import tqdm
import numpy as np
import torch
import redis

from utils import line_count, pad_1d, pad_2d, append_storage, resize_storage

import ipdb

DEFAULT_VOCAB = ['_PAD', '_NAF', '_UNK', '_SOS', '_EOS']
PAD_IDX, NAF_IDX, UNK_IDX, SOS_IDX, EOS_IDX = 0, 1, 2, 3, 4
NAF_TRIPLE = [NAF_IDX, NAF_IDX, NAF_IDX]


class DistributedBatchSampler(DistributedSampler):
    def __init__(self, dataset, num_replicas=None, rank=None, shuffle=True, batch_access=1):
        if num_replicas is None:
            if not dist.is_available():
                raise RuntimeError("Requires distributed package to be available")
            num_replicas = dist.get_world_size()
        if rank is None:
            if not dist.is_available():
                raise RuntimeError("Requires distributed package to be available")
            rank = dist.get_rank()
        self.dataset = dataset
        self.num_replicas = num_replicas
        self.rank = rank
        self.epoch = 0
        self.num_samples = int(math.ceil(len(self.dataset) * 1.0 / (self.num_replicas * batch_access)))
        self.total_size = self.num_samples * self.num_replicas * batch_access
        self.shuffle = shuffle
        self.batch_access = batch_access

    def __iter__(self):
        # deterministically shuffle based on epoch
        g = torch.Generator()
        g.manual_seed(self.epoch)
        if self.shuffle:
            indices = torch.randperm(len(self.dataset), generator=g).tolist()
        else:
            indices = list(range(len(self.dataset)))

        # add extra samples to make it evenly divisible
        indices += indices[:(self.total_size - len(indices))]
        assert len(indices) == self.total_size

        # subsample
        indices = indices[self.rank*self.batch_access:self.total_size:self.num_replicas*self.batch_access]
        assert len(indices) == self.num_samples
        return iter(indices)


def get_dataloader(args,
                   data_path='data',
                   data_name='train',
                   batch_size=128,
                   shuffle=True,
                   num_workers=4):
    dataset = CommonsenseDialDataset(args, data_path, data_name)
    batch_size = batch_size // args.batch_access
    num_replicas = args.world_size if data_name == 'train' else 1
    sampler = DistributedBatchSampler(dataset=dataset, num_replicas=num_replicas, rank=args.local_rank, shuffle=shuffle, batch_access=args.batch_access)
    data_loader = torch.utils.data.DataLoader(dataset=dataset,
                                            batch_size=batch_size,
                                            num_workers=num_workers,
                                            pin_memory=True,
                                            collate_fn=collate_fn,
                                            sampler=sampler
                                            )
    return data_loader


class CommonsenseDialDataset(torch.utils.data.Dataset):
    def __init__(self, args, data_path='data', data_name='train'):
        assert data_name in ['train', 'test', 'valid'], "Data name should be among ['train', 'test', 'valid']."
        self.args = args
        self.data_path = data_path
        self.batch_access = args.batch_access
        
        data_dump = f'{self.data_path}/{data_name}set_new.zarr'
        vocab_file = f'{self.data_path}/vocab.pkl'

        self.rel2idx = self.make_rel_vocab()
        self.idx2rel = {val: key for key, val in self.rel2idx.items()}

        if not os.path.isfile(vocab_file):
            self.init_vocab() # only used for init_data
        else:
            with open(vocab_file, 'rb') as vf:
                d = pickle.load(vf)
                self.word2idx = d['word2idx']
                self.entidx2wordidx = d['entidx2wordidx']

        if not os.path.exists(data_dump):
            self.idx2triple = self.make_triple_vocab()
            self.init_data(data_name)
        
        self.data = zarr.open(data_dump, mode='r') # load zarr dump
        self.idx2word = OrderedDict([(v, k) for k, v in self.word2idx.items()])
        self.triple_dict = self.make_triple_dict()
        self.entity_lst = self.entidx2wordidx.values()
        self.rd = redis.StrictRedis()
        

    def init_vocab(self):
        # First add DEFAULT_VOCAB
        # idx of each word/entity: glove에서의 idx + 5
        self.word2idx = OrderedDict([*zip(DEFAULT_VOCAB, range(len(DEFAULT_VOCAB)))])
        
        # Then add Glove vocabs (30000)
        with open(f'{self.data_path}/glove.840B.300d.txt', 'r') as glove_f:
            for i, line in enumerate(glove_f):
                if i >= self.args.n_glove_vocab:
                    break
                k = line.split()[0]
                self.word2idx[k] = len(self.word2idx)

        # Now add entity vocab that are not in glove
        self.entidx2wordidx = {} # maps entity idx in 'resources.txt' to word idx
        raw_dict = open(f'{self.data_path}/resource.txt', 'r').read()
        raw_dict = literal_eval(raw_dict)
        for ent, idx in raw_dict['dict_csk_entities'].items():
            if ent not in self.word2idx:
                self.word2idx[ent] = len(self.word2idx)
            self.entidx2wordidx[idx] = self.word2idx[ent]

        # Store vocab
        print(f'Vocab size: {len(self.word2idx)}')
        vocab = {'word2idx': self.word2idx,
                'entidx2wordidx': self.entidx2wordidx}
        with open(f'{self.data_path}/vocab.pkl', 'wb') as df:
            pickle.dump(vocab, df)
        
            
    def init_data(self, data_name, n_chunk=1024):
        print(f'Initializing {data_name} data...')

        def transform_triple_to_hrt(triple_idx):
            """ Transforms triple-idx (as a whole) to h/r/t format """
            if triple_idx == -1: # for response_triple
                return NAF_TRIPLE
            triple = self.idx2triple[triple_idx]
            h, r, t = triple.split(', ')
            return [self.word2idx[h], self.rel2idx[r], self.word2idx[t]]

        def process_file(root, inp):
            start_i, filename = inp
            n_sample = line_count(filename)
            
            post = np.zeros((n_sample, self.args.max_sentence_len), dtype=np.int32)
            post_length = np.zeros((n_sample), dtype=np.int32) # valid length (without pad)
            response = np.zeros((n_sample, self.args.max_sentence_len), dtype=np.int32)
            response_length = np.zeros((n_sample), dtype=np.int32)
            # post_triple = np.zeros((n_sample, self.args.max_sentence_len), dtype=np.int32)
            triple = np.zeros((n_sample, self.args.max_sentence_len, self.args.max_triple_len, 3), dtype=np.int32)
            entity = np.zeros((n_sample, self.args.max_sentence_len, self.args.max_triple_len), dtype=np.int32)
            response_triple = np.zeros((n_sample, self.args.max_sentence_len, 3), dtype=np.int32)

            max_post_len, max_response_len, max_triple_len = 0, 0, 0

            with jsonlines.open(filename) as df:
                for i, line in enumerate(df):

                    pl, rl = len(line['post']) + 2, len(line['response']) + 2
                    post_length[i] = pl
                    response_length[i] = rl

                    max_post_len = max(pl, max_post_len) 
                    max_response_len = max(rl, max_response_len)
                    max_triple_len = max([len(l) for l in line['all_triples']] + [max_triple_len])

                    all_triples = [line['all_triples'][i-1] if i > 0 else [-1] for i in line['post_triples']]

                    post[i, :pl] = [SOS_IDX] + [self.get_word_idx(p) for p in line['post']] + [EOS_IDX]
                    response[i, :rl] = [SOS_IDX] + [self.get_word_idx(r) for r in line['response']] + [EOS_IDX]
                    # post_triple[i, 1:pl-1] = np.array(line['post_triples']) # [0, 0, 1, 0, 2...]
                    response_triple[i, :rl] = [NAF_TRIPLE] + [transform_triple_to_hrt(rt) for rt in line['response_triples']] + [NAF_TRIPLE]

                    # put NAF_TRIPLE/entity at index 0
                    triple[i] = pad_2d([[NAF_TRIPLE]] + [[transform_triple_to_hrt(t) for t in triples] for triples in all_triples] + [[NAF_TRIPLE]], length=(self.args.max_sentence_len, self.args.max_triple_len, 3))
                    entity[i] = pad_2d([[NAF_IDX]] + [[self.entidx2wordidx[e] for e in entities] for entities in line['all_entities']] + [[NAF_IDX]], length=(self.args.max_sentence_len, self.args.max_triple_len))

                # dump to zarr
                root['post'][start_i : start_i+n_sample] = post
                root['post_length'][start_i : start_i+n_sample] = post_length
                root['response'][start_i : start_i+n_sample] = response
                root['response_length'][start_i : start_i+n_sample] = response_length
                # root['post_triple'][start_i : start_i+n_sample] = post_triple
                root['triple'][start_i : start_i+n_sample] = triple
                root['entity'][start_i : start_i+n_sample] = entity
                root['response_triple'][start_i : start_i+n_sample] = response_triple
                
            return max_post_len, max_response_len, max_triple_len

        
        toread = [f'{self.data_path}/{data_name}set_pieces/{piece}' for piece in os.listdir(f'{self.data_path}/{data_name}set_pieces')]
        n_lines = sum([line_count(piece) for piece in toread])
        init_n_lines = math.ceil(n_lines / n_chunk) * n_chunk # 마지막 조각 사이즈가 지정된 청크 사이즈보다 작아져서 나는 에러 방지

        root = zarr.open(f'{self.data_path}/{data_name}set_new.zarr', mode='w')
        post = root.zeros('post', shape=(init_n_lines, self.args.max_sentence_len), chunks=(n_chunk, None), dtype='i4')
        post_length = root.zeros('post_length', shape=(init_n_lines,), chunks=(n_chunk,), dtype='i4') # valid length (without pad)
        response = root.zeros('response', shape=(init_n_lines, self.args.max_sentence_len), chunks=(n_chunk, None), dtype='i4')
        response_length = root.zeros('response_length', shape=(init_n_lines,), chunks=(n_chunk,), dtype='i4')
        post_triple = root.zeros('post_triple', shape=(init_n_lines, self.args.max_sentence_len), chunks=(n_chunk, None), dtype='i4')
        triple = root.zeros('triple', shape=(init_n_lines, self.args.max_sentence_len, self.args.max_triple_len, 3), chunks=(n_chunk, None, None, None), dtype='i4')
        entity = root.zeros('entity', shape=(init_n_lines, self.args.max_sentence_len, self.args.max_triple_len), chunks=(n_chunk, None, None), dtype='i4')
        response_triple = root.zeros('response_triple', shape=(init_n_lines, self.args.max_sentence_len, 3), chunks=(n_chunk, None, None), dtype='i4')

        pool = Pool(min(len(toread), mp.cpu_count()))
        func = functools.partial(process_file, root)
        iterinp = [(i*self.args.data_piece_size, filename) for i, filename in enumerate(toread)]
        max_post_lens, max_response_lens, max_triple_lens = zip(*tqdm(pool.imap(func, iterinp), total=len(iterinp)))

        max_post_len, max_response_len, max_triple_len = max(max_post_lens), max(max_response_lens), max(max_triple_lens)

        # trim remaining space
        post.resize(n_lines, max_post_len)
        post_length.resize(n_lines)
        response.resize(n_lines, max_response_len)
        response_length.resize(n_lines)
        post_triple.resize(n_lines, max_post_len)
        triple.resize(n_lines, max_post_len, max_triple_len, 3)
        entity.resize(n_lines, max_post_len, max_triple_len)
        response_triple.resize(n_lines, max_response_len, 3)

        print(f'Dumped {data_name} at: {self.data_path}/{data_name}set_new.zarr')

    def make_rel_vocab(self):
        # Don't dump; call every time
        rel2idx = {'_PAD': PAD_IDX, '_NAF': NAF_IDX}
        with open(f'{self.data_path}/relation.txt', 'r') as rel_f:
            rel_dict = {line.strip(): i for i, line in enumerate(rel_f, start=len(rel2idx))}
            rel2idx.update(rel_dict)
        return rel2idx

    def make_triple_vocab(self):
        raw_dict = open(f'{self.data_path}/resource.txt', 'r').read()
        raw_dict = literal_eval(raw_dict)
        idx2triple = {v: k for k, v in raw_dict['dict_csk_triples'].items()}
        return idx2triple

    def make_triple_dict(self):
        raw_dict = open(f'{self.data_path}/resource.txt', 'r').read()
        raw_dict = literal_eval(raw_dict)
        triple_dict = defaultdict(lambda: [NAF_TRIPLE])
        for k, triples in raw_dict['dict_csk'].items():
            tmp = []
            for tr in triples:
                h, r, t = tr.split(", ")
                tmp.append([self.word2idx[h], self.rel2idx[r], self.word2idx[t]])
            triple_dict[self.word2idx[k]] = tmp
        return triple_dict

    def __len__(self):
        return len(self.data['post'])

    def __getitem__(self, i):
        return {k: v[i:i+self.batch_access] for k, v in self.data.arrays()}

    def get_word_idx(self, word):
        res = self.word2idx.get(word, UNK_IDX)
        if res >= self.args.n_glove_vocab + len(DEFAULT_VOCAB):
            res = UNK_IDX
        return res

    def retrieve_graph(self, query_idx):
        if query_idx not in self.entity_lst:
            return [NAF_TRIPLE]
        query = self.idx2word[query_idx]
        query_as_head = self.rd.execute_command('GRAPH.QUERY', 'CCM', f"MATCH (x)-[r]->(y) WHERE x.word = '{query}' RETURN r, y.word")
        query_as_tail = self.rd.execute_command('GRAPH.QUERY', 'CCM', f"MATCH (x)-[r]->(y) WHERE y.word = '{query}' RETURN r, x.word")
        query_as_head = [(rel[1][1].decode('utf-8'), ent.decode('utf-8')) for rel, ent in query_as_head[1]]
        query_as_tail = [(rel[1][1].decode('utf-8'), ent.decode('utf-8')) for rel, ent in query_as_tail[1]]
        return [[query_idx, self.rel2idx[r], self.word2idx[e]] for r, e in query_as_head] + [[self.word2idx[e], self.rel2idx[r], query_idx] for r, e in query_as_tail]


def collate_fn(batch):
    post = torch.cat([torch.from_numpy(s['post']) for s in batch], 0) # (bsz, pl)
    post_length = torch.cat([torch.from_numpy(s['post_length']) for s in batch], 0) # (bsz,)
    response = torch.cat([torch.from_numpy(s['response']) for s in batch], 0) # (bsz, rl)
    response_length = torch.cat([torch.from_numpy(s['response_length']) for s in batch], 0) # (bsz,)
    post_triple = torch.cat([torch.from_numpy(s['post_triple']) for s in batch], 0) # (bsz, pl)
    triple = torch.cat([torch.from_numpy(s['triple']) for s in batch], 0) # (bsz, pl, tl, 3) # NOTE: 원래는 pl보다 작지만 (valid-pl-with-triple이므로) 그냥 똑같이 pl로 둠
    entity = torch.cat([torch.from_numpy(s['entity']) for s in batch], 0) # (bsz, pl, tl)
    response_triple = torch.cat([torch.from_numpy(s['response_triple']) for s in batch], 0) # (bsz, rl, 3)

    # HACK to resolve NaN issue (data that are all 0)
    is_nonzero = np.where(triple.view(triple.size(0), -1).sum(-1))
    post, post_length, response, response_length, post_triple, triple, entity, response_triple = \
        post[is_nonzero], post_length[is_nonzero], response[is_nonzero], response_length[is_nonzero], post_triple[is_nonzero], triple[is_nonzero], entity[is_nonzero], response_triple[is_nonzero]

    # Sort in descending length order
    perm_idx = torch.sort(post_length, descending=True)[1].long()
    post, post_length, response, response_length, post_triple, triple, entity, response_triple = \
        post[perm_idx], post_length[perm_idx], response[perm_idx], response_length[perm_idx], post_triple[perm_idx], triple[perm_idx], entity[perm_idx], response_triple[perm_idx]

    max_pl = post_length[0]
    max_rl = torch.max(response_length)
    max_tl = torch.max((entity == 0).sum(-1))

    post = post[:, :max_pl]
    response = response[:, :max_rl]
    post_triple = post_triple[:, :max_pl]
    triple = triple[:, :max_pl, :max_tl]
    entity = entity[:, :max_pl, :max_tl]
    response_triple = response_triple[:, :max_rl]

    batched_data = {
        'post': post.long(),
        'post_length': post_length,
        'response': response.long(),
        'response_length': response_length,
        'post_triple': post_triple.long(),
        'triple': triple.long(),
        'entity': entity,
        'response_triple': response_triple.long(),
    }

    return batched_data


if __name__ == "__main__":
    args = {'max_sentence_len': 150, 'max_triple_len': 50, 'data_piece_size': 10000}
    class Args(object):
        def __init__(self, adict):
            self.__dict__.update(adict)
    args = Args(args)
    dataloader = get_dataloader(args=args, batch_size=2, shuffle=False)
    batch = iter(dataloader).next()

    for k, v in batch.items():
        print(k, v.shape)
