import csv
import argparse
import random
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.init import kaiming_uniform_
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence, PackedSequence
import pandas as pd
import numpy as np
from torch_scatter import scatter_add
from dataset import DEFAULT_VOCAB, PAD_IDX, NAF_IDX, UNK_IDX, SOS_IDX, EOS_IDX


def get_pretrained_glove(path, n_word=30000):
    saved_glove = path.replace('.txt', '.pt')
    if not os.path.isfile(saved_glove):
        print('Reading pretrained glove...')
        words = pd.read_csv(path, sep=" ", index_col=0, header=None, quoting=csv.QUOTE_NONE, nrows=n_word)
        # def get_vec(w):
        #     return words.loc[w].values.astype('float32')
        # weights = [torch.from_numpy(get_vec(w)).unsqueeze(0) for i, w in enumerate(DEFAULT_VOCAB)]
        # weights.append(torch.from_numpy(words.iloc[:n_word, :].values.astype('float32')))
        weights = [torch.from_numpy(words.values.astype('float32'))]
        default = [torch.rand(1, weights[0].size(-1)) for i, w in enumerate(DEFAULT_VOCAB)]
        weights = torch.cat(default + weights, dim=0)
        torch.save(weights, saved_glove)
        print(f"Glove saved in {saved_glove}")
    return torch.load(saved_glove)


def get_pretrained(label_path, weight_path, idx2word, dim=100):
    saved_weight = weight_path.replace('.txt', '.pt')
    if not os.path.isfile(saved_weight):
        labels = [label for label in open(label_path, 'r').read().split('\n') if label]
        entity = pd.read_csv(weight_path, sep="\t", header=None, quoting=csv.QUOTE_NONE)
        entity.index = labels
        n = 0

        def get_vec(w):
            nonlocal n
            try:
                return entity.loc[w].values.astype('float32')[:dim]
            except KeyError:
                # print(w)
                n += 1
                return np.random.rand(dim).astype('float32')

        weights = [torch.from_numpy(get_vec(w)).unsqueeze(0) for i, w in idx2word.items()]
        print(n, len(idx2word))
        weights = torch.cat(weights, dim=0)
        torch.save(weights, saved_weight)
        print(f"Weights saved in {saved_weight}")
    return torch.load(saved_weight)


def get_pad_mask(lengths, max_length):
    """ 1 for pad """
    bsz = lengths.size(0)
    mask = torch.zeros((bsz, max_length), dtype=torch.bool)
    for j in range(bsz):
        mask[j, lengths[j]:] = 1
    return mask


class CCMModel(nn.Module):
    def __init__(self, args, dataset):
        super().__init__()
        self.args = args
        self.dataset = dataset
        self.n_glove_vocab = args.n_glove_vocab + len(DEFAULT_VOCAB) # glove only
        self.n_out_vocab = len(self.dataset.idx2word)
        self.gru_layer = args.gru_layer
        self.t_embed = args.t_embed
        self.teacher_forcing = args.teacher_forcing
        self.max_response_len = args.max_response_len

        self.word_embedding = nn.Embedding.from_pretrained(
            get_pretrained_glove(path=f'{args.data_dir}/glove.840B.300d.txt', n_word=args.n_glove_vocab),
            freeze=False, padding_idx=PAD_IDX) # specials: pad, unk, naf_h/t

        self.entity_embedding = nn.Embedding.from_pretrained(
            get_pretrained(label_path=f'{args.data_dir}/entity.txt', weight_path=f'{args.data_dir}/entity_transE.txt', idx2word=self.dataset.idx2word),
            freeze=False, padding_idx=PAD_IDX)

        self.rel_embedding = nn.Embedding.from_pretrained(
            get_pretrained(label_path=f'{args.data_dir}/relation.txt', weight_path=f'{args.data_dir}/relation_transE.txt', idx2word=self.dataset.idx2rel),
            freeze=False, padding_idx=PAD_IDX)

        self.MLP = nn.Linear(3 * args.t_embed, 3 * args.t_embed)
        self.Wh = nn.Linear(args.t_embed, args.hidden)
        self.Wr = nn.Linear(args.t_embed, args.hidden)
        self.Wt = nn.Linear(args.t_embed, args.hidden)
        self.gru_enc = nn.GRU(args.d_embed + 2 * args.t_embed, args.gru_hidden, args.gru_layer, batch_first=True)
        self.gru_dec = nn.GRU(args.gru_hidden + 8 * args.t_embed + args.d_embed, args.gru_hidden, args.gru_layer, batch_first=True)
        self.Wa = nn.Linear(args.gru_hidden * args.gru_layer, args.gru_hidden)
        self.Wb = nn.Linear(args.gru_hidden * args.gru_layer, args.hidden)
        self.Ub = nn.Linear(2 * args.t_embed, args.hidden)
        self.Vb = nn.Linear(args.hidden, 1)
        self.Wc = nn.Linear(args.gru_hidden * args.gru_layer, 3 * args.t_embed)

        self.Vo = nn.Linear(3 * args.gru_hidden + 5 * args.t_embed, 1)
        self.Wo = nn.Linear(3 * args.gru_hidden + 5 * args.t_embed, self.n_glove_vocab)

    def retrieve_graph(self, query):
        out = []
        for q in query.view(-1):
            ret = self.dataset.retrieve_graph(q.item())
            out.append(ret)
            
    def forward(self, batch):
        post = batch['post']
        bsz = post.size()[0]
        post_mask = post.eq(PAD_IDX)
        post_length = batch['post_length']
        # post_triple = batch['post_triple']
        triple = batch['triple']
        triple_mask = triple.eq(PAD_IDX)
        entity = batch['entity']
        device = post.device

        post_emb = self.word_embedding(post)  # (bsz, pl, d_embed)
        head, rel, tail = torch.split(triple, 1, 3)  # (bsz, pl, tl)
        head_emb = self.entity_embedding(head.squeeze(-1))  # (bsz, pl, tl, t_embed)
        rel_emb = self.rel_embedding(rel.squeeze(-1)) # (bsz, pl, tl, t_embed)
        tail_emb = self.entity_embedding(tail.squeeze(-1))  # (bsz, pl, tl, t_embed)
        triple_emb = self.MLP(torch.cat([head_emb, rel_emb, tail_emb], 3))  # (bsz, pl, tl, 3 * t_embed)


        response = batch['response']
        response[response >= self.n_glove_vocab] = UNK_IDX
        rl = response.size()[1]
        response_triple = batch['response_triple']
        if not self.training:
            response = torch.ones((bsz, 1), dtype=torch.long, device=device) * SOS_IDX
            response_triple = torch.ones((bsz, 1, 3), dtype=torch.long, device=device) * NAF_IDX
        response_emb = self.word_embedding(response)  # (bsz, rl, d_embed)
        res_head, res_rel, res_tail = torch.split(response_triple, 1, 2)  # (bsz, rl, 1)
        res_head_emb = self.entity_embedding(res_head.squeeze(-1))  # (bsz, rl, t_embed)
        res_rel_emb = self.rel_embedding(res_rel.squeeze(-1))  # (bsz, rl, t_embed)
        res_tail_emb = self.entity_embedding(res_tail.squeeze(-1))  # (bsz, rl, t_embed)
        res_triple_emb = self.MLP(torch.cat([res_head_emb, res_rel_emb, res_tail_emb], 2))  # (bsz, rl, 3 * t_embed)

        # Static Graph
        ent = torch.cat([head_emb, tail_emb], -1)  # (bsz, pl, tl, 2 * t_embed)
        # mask = get_pad_mask(post_triple.max(-1)[0], ent.size(1)).to(device)
        # ent.data.masked_fill_(mask.view(*mask.size(), 1, 1), 0)
        static_logit = (self.Wr(rel_emb) * torch.tanh(self.Wh(head_emb) + self.Wt(tail_emb))).sum(-1, keepdim=False)  # (bsz, pl, tl)
        static_logit.data.masked_fill_(triple_mask[:, :, :, 0], -float('inf'))
        static_logit.data.masked_fill_(post_mask.unsqueeze(-1), 0)
        static_attn = F.softmax(static_logit, dim=-1)  # (bsz, pl, tl) # TODO: NAN
        static_graph = (ent * static_attn.unsqueeze(-1)).sum(-2)  # (bsz, pl, 2 * t_embed) / gi
        # post_triples = static_graph.gather(1, post_triple.unsqueeze(-1).expand_as(static_graph))
        post_input = torch.cat([post_emb, static_graph], -1)  # (bsz, pl, d_emb + 2 * t_embed)

        # Encoder
        packed_post_input = pack_padded_sequence(post_input, lengths=post_length.tolist(), batch_first=True)
        packed_post_output, gru_hidden = self.gru_enc(packed_post_input)
        post_output, _ = pad_packed_sequence(packed_post_output, batch_first=True)  # (bsz, pl, go)
        gru_state = gru_hidden.transpose(0, 1).reshape(bsz, 1, -1)

        # Decoder
        dec_logits = []
        pointer_probs = []
        t = 0
        response_input = torch.cat([response_emb, res_triple_emb], -1)  # (bsz, rl, d_embed + 3 * t_embed)
        response_vector = response_input[:, 0]  # (bsz, d_embed + 3 * t_embed)
        finished_index = torch.zeros(bsz, device=device)
        while True:
            # c
            context_logit = (post_output * self.Wa(gru_state)).sum(-1)  # (bsz, pl)
            context_logit.data.masked_fill_(post_mask, -float('inf'))
            context_attn = F.softmax(context_logit, dim=-1)  # (bsz, pl)
            context_vector = (post_output * context_attn.unsqueeze(-1)).sum(-2, keepdim=False)  # (bsz, gru_hidden) / c

            # cg
            dynamic_logit = self.Vb(torch.tanh(self.Wb(gru_state) + self.Ub(static_graph))).squeeze(-1)  # (bsz, pl)
            dynamic_logit.data.masked_fill_(post_mask, -float('inf'))
            dynamic_attn = F.softmax(dynamic_logit, dim=-1)  # (bsz, pl)
            dynamic_graph = (static_graph * dynamic_attn.unsqueeze(-1)).sum(-2)  # (bsz, 2 * t_embed) / cg

            # ck
            triple_logit = (triple_emb * self.Wc(gru_state).unsqueeze(-2)).sum(-1)  # (bsz, pl, tl)
            triple_logit.data.masked_fill_(triple_mask[:, :, :, 0], -float('inf'))
            triple_logit.data.masked_fill_(post_mask.unsqueeze(-1), 0)
            triple_attn = F.softmax(triple_logit, dim=-1)  # (bsz, pl, tl)
            triple_tmp = (triple_emb * triple_attn.unsqueeze(-1)).sum(-2, keepdim=False)
            triple_tmp.data.masked_fill_(post_mask.unsqueeze(-1), 0)
            triple_vector = (triple_tmp * dynamic_attn.unsqueeze(-1)).sum(-2)  # (bsz, 3 * t_embed)

            dec_input = torch.cat([context_vector, dynamic_graph, triple_vector, response_vector], 1).unsqueeze(
                -2)  # (bsz, gru_hidden + 8 * t_embed + d_embed)
            gru_out, gru_hidden = self.gru_dec(dec_input,
                                               gru_hidden)  # (bsz, 1, gru_hidden) / (2*gru_hidden, bsz) # NOTE: 2-layer..
            gru_state = gru_hidden.transpose(0, 1).reshape(bsz, 1, -1)

            # pointer-generator logic
            final_dist_input = torch.cat([gru_state.squeeze(1), context_vector, dynamic_graph, triple_vector], dim=-1) # (bsz, 3*gru_hidden + 5*t_embed)
            generic_dist = F.softmax(self.Wo(final_dist_input), -1) # (bsz, n_vocab)
            entity_dist = dynamic_attn.unsqueeze(-1) * triple_attn # (bsz, pl, tl)
            pointer_prob = torch.sigmoid(self.Vo(final_dist_input))
            pointer_probs.append(pointer_prob)
            dists = torch.cat([(1 - pointer_prob) * generic_dist, pointer_prob * entity_dist.view(bsz, -1)], -1) 
            indices = torch.cat([
              torch.arange(self.n_glove_vocab).repeat(bsz, 1).to(entity),
              entity.view(bsz, -1)
              ], -1)
            out = dists.new_zeros((bsz, self.n_out_vocab))
            final_dist = scatter_add(dists, indices.long(), out=out)

            dec_logits.append(final_dist.unsqueeze(0))

            if random.random() < self.teacher_forcing and self.training:
                response_vector = response_input[:, t + 1] # ground truth
            else:
                top1 = final_dist.max(-1)[1]  # (bsz, )
                top1[top1 >= self.n_glove_vocab] = UNK_IDX
                finished_index[top1 == EOS_IDX] = 1
                response_emb = self.word_embedding(top1)  # (bsz, d_embed)
                top1_triple_idx = entity_dist.view(bsz, -1).max(-1)[1]
                top1_triple_emb = triple_emb.view(bsz, -1, triple_emb.size(-1))[torch.arange(bsz), top1_triple_idx]
                response_vector = torch.cat([response_emb, top1_triple_emb], -1)  # (bsz, d_embed + 3 * t_embed)
            t += 1
            if (self.training and t == rl-1) or \
                    (not self.training and (finished_index.sum() == bsz or t == self.max_response_len)):
                break

        dec_logits = torch.cat(dec_logits, 0).permute(1, 2, 0)
        pointer_probs = torch.cat(pointer_probs, -1)
        return dec_logits, pointer_probs


class Baseline(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.n_glove_vocab = args.n_glove_vocab + len(DEFAULT_VOCAB) # glove only
        self.gru_layer = args.gru_layer
        self.teacher_forcing = args.teacher_forcing
        self.max_response_len = args.max_response_len

        self.word_embedding = nn.Embedding.from_pretrained(
            get_pretrained_glove(path=f'{args.data_dir}/glove.840B.300d.txt', n_word=args.n_glove_vocab),
            freeze=False, padding_idx=PAD_IDX) # specials: pad, unk, naf_h/t
        self.gru_enc = nn.GRU(args.d_embed, args.gru_hidden, args.gru_layer, batch_first=True)
        self.gru_dec = nn.GRU(args.d_embed, args.gru_hidden, args.gru_layer, batch_first=True)
        self.Wo = nn.Linear(args.gru_hidden, self.n_glove_vocab)

    def forward(self, batch):
        post = batch['post']
        post_length = batch['post_length']
        response = batch['response']
        bsz = post.size()[0]
        device = post.device
        rl = response.size()[1]

        post_emb = self.word_embedding(post)  # (bsz, pl, d_embed)
        response[response >= self.n_glove_vocab] = UNK_IDX
        response_emb = self.word_embedding(response)  # (bsz, rl, d_embed)

        # Encoder
        packed_post_input = pack_padded_sequence(post_emb, lengths=post_length.tolist(), batch_first=True)
        packed_post_output, gru_hidden = self.gru_enc(packed_post_input)
        post_output, _ = pad_packed_sequence(packed_post_output, batch_first=True)  # (bsz, pl, go)

        # Decoder
        dec_logits = []
        t = 0
        finished_index = torch.zeros((bsz,1), device=device)
        response_input = response_emb[:, 0:1]
        while True:
            t += 1
            gru_out, gru_hidden = self.gru_dec(response_input, gru_hidden)  # (bsz, 1, gru_hidden) / (2*gru_hidden, bsz) # NOTE: 2-layer..
            dec_logit = F.softmax(self.Wo(gru_out), -1) # (bsz, n_vocab)
            dec_logits.append(dec_logit)

            if random.random() < self.teacher_forcing and self.training:
                response_input = response_emb[:, t:t+1] # ground truth
            else:
                top1 = dec_logit.max(-1)[1]  # (bsz, )
                top1[top1 >= self.n_glove_vocab] = UNK_IDX
                finished_index[top1 == EOS_IDX] = 1
                response_input = self.word_embedding(top1)  # (bsz, d_embed)
            if (self.training and t == rl-1) or \
                    (not self.training and (finished_index.sum() == bsz or t == self.max_response_len)):
                break
        dec_logits = torch.cat(dec_logits, 1).transpose(1, 2)
        return dec_logits, None

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='parser')
    parser.add_argument('--data_dir', type=str, default='data')
    parser.add_argument('--d_embed', type=int, default=300)
    parser.add_argument('--t_embed', type=int, default=100)
    parser.add_argument('--hidden', type=int, default=128)
    parser.add_argument('--n_glove_vocab', type=int, default=30000)
    parser.add_argument('--n_entity_vocab', type=int, default=22590)
    parser.add_argument('--gru_layer', type=int, default=2)
    parser.add_argument('--gru_hidden', type=int, default=512)
    parser.add_argument('--batch_size', type=int, default=4)
    parser.add_argument('--max_sentence_len', type=int, default=150)
    parser.add_argument('--max_triple_len', type=int, default=50)
    parser.add_argument('--max_response_len', type=int, default=150)
    parser.add_argument('--init_chunk_size', type=int, default=10000)
    parser.add_argument('--teacher_forcing', type=float, default=0.5)
    parser.add_argument('--seed', type=int, default=41)
    args = parser.parse_args()

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    from dataloader import get_dataloader
    dataloader = get_dataloader(args=args, batch_size=args.batch_size, shuffle=False)

    # model = CCMModel(args, dataloader.dataset.idx2ent, dataloader.dataset.idx2rel)
    model = Baseline(args, dataloader.dataset.idx2word, dataloader.dataset.idx2rel)
    batch = iter(dataloader).next()
    model(batch)