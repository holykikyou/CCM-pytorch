import argparse
import datetime

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from tensorboardX import SummaryWriter

from dataset import get_dataloader, PAD_IDX, NAF_IDX
from model import CCMModel
from recorder import Recorder
from criterion import criterion, perplexity

import ipdb


def epoch(epoch_idx, is_train):
    model.train() if is_train else model.eval()
    loader = train_loader if is_train else val_loader
    recorder.epoch_start(epoch_idx, is_train, loader)
    for batch_idx, batch in enumerate(loader):
        batch_size, rl = batch['response'].size()
        batch = {key: val.to(device) for key, val in batch.items()}
        optimizer.zero_grad()
        output, pointer_prob = model(batch)
        output_len = output.size()[2]
        if output_len > rl-1:
            output = output[:, :, :rl-1]
            pointer_prob = pointer_prob[:, :rl-1]
        elif output_len < rl-1:
            pad = torch.zeros((batch_size, output.size()[1], rl-1), device=device)
            pad[:, :, :output_len] = output
            output = pad
            pad = torch.zeros((batch_size, rl - 1), device=device)
            pad[:, :output_len + 1] = pointer_prob
            pointer_prob = pad
        pointer_prob_target = (batch['response_triple'] != NAF_IDX).all(-1).to(pointer_prob)
        pointer_prob_target.data.masked_fill_(batch['response'] == 0, PAD_IDX)
        loss, nll_loss = criterion(output, pointer_prob, batch['response'][:, 1:], pointer_prob_target[:, 1:])
        pp = perplexity(nll_loss)
        if is_train:
            loss.backward()
            optimizer.step()
        recorder.batch_end(batch_idx, batch_size, loss.item(), pp.item())
    recorder.log_text(output, batch)
    recorder.epoch_end()


def train():
    for epoch_idx in range(1, args.epochs + 1):
        epoch(epoch_idx, is_train=True)
        epoch(epoch_idx, is_train=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='parser')
    parser.add_argument('--project', type=str, default='ccm')
    parser.add_argument('--timestamp', type=str, default=datetime.datetime.now().strftime("%y%m%d%H%M%S"))
    parser.add_argument('--data_dir', type=str, default='data')
    parser.add_argument('--log_dir', type=str, default='log')
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--log_interval', type=int, default=100)
    parser.add_argument('--teacher_forcing', type=float, default=1.0)
    parser.add_argument('--d_embed', type=int, default=300)
    parser.add_argument('--t_embed', type=int, default=100)
    parser.add_argument('--hidden', type=int, default=128)
    parser.add_argument('--n_glove_vocab', type=int, default=30000)
    parser.add_argument('--n_entity_vocab', type=int, default=22590)
    parser.add_argument('--gru_layer', type=int, default=2)
    parser.add_argument('--gru_hidden', type=int, default=512)
    parser.add_argument('--max_sentence_len', type=int, default=150)
    parser.add_argument('--max_triple_len', type=int, default=50)
    parser.add_argument('--max_response_len', type=int, default=150)
    parser.add_argument('--data_piece_size', type=int, default=10000)
    parser.add_argument('--seed', type=int, default=41)
    parser.add_argument('--num_workers', type=int, default=8)
    parser.add_argument('--no_cuda', action='store_true')
    parser.add_argument('--cuda', type=int, default=0)
    args = parser.parse_args()

    device = torch.device(f"cuda:{args.cuda}" if not args.no_cuda and torch.cuda.is_available() else "cpu")
    torch.manual_seed(args.seed)

    train_loader = get_dataloader(args, data_path=args.data_dir, data_name='train', batch_size=args.batch_size//16, num_workers=args.num_workers)
    val_loader = get_dataloader(args, data_path=args.data_dir, data_name='valid', batch_size=args.batch_size//16, num_workers=args.num_workers)
    model = CCMModel(args, train_loader.dataset)
    model = model.to(device)
    optimizer = optim.Adam(model.parameters(), args.lr)
    writer = SummaryWriter(f'{args.log_dir}/{args.project}_{args.timestamp}')
    recorder = Recorder(args, writer, train_loader.dataset.idx2word)

    train()
