import torch
import numpy as np
import math
import time

from collections import defaultdict
from torch.autograd import Variable
from tqdm import tqdm, trange
from utils import *
from decoder import valid_model

import torch.distributed as dist


def train_model(args, watcher, model, train, dev, save_path=None, maxsteps=None, decoding_path=None, names=None):

    # optimizer
    opt_all = [torch.optim.Adam(param, betas=(0.9, 0.98), eps=1e-9) for param in model.module.trainable_parameters()]
    opt = opt_all[0]

    if args.model == 'AutoTransformer2':
        opt = opt_all[1]

    # if resume training
    if (args.load_from != 'none') and (args.resume):
        with torch.cuda.device(args.local_rank):   # very important.
            offset, opt_states = torch.load(args.workspace_prefix + '/models/' + args.load_from + '.pt.states',
                                            map_location=lambda storage, loc: storage.cuda())
            opt.load_state_dict(opt_states)
    else:
        offset = 0
    
    iters = offset
    best_i = 0

    # confirm the saving path
    if save_path is None:
        save_path = args.model_name

    # setup a watcher
    param_to_watch = ['corpus_bleu']
    watcher.set_progress_bar(args.eval_every)
    watcher.set_best_tracker(model, opt, save_path, args.local_rank, *param_to_watch)
    if args.tensorboard and (not args.debug):
        watcher.set_tensorboard('{}/runs/{}'.format(args.workspace_prefix, args.prefix+args.hp_str))


    train = [iter(t) for t in train]
    while True:

        def check(every, k=0):
            return iters % every == k

        # --- saving --- #
        if check(args.save_every) and (args.local_rank == 0): # saving only works for local-rank=0
            watcher.info('save (back-up) checkpoints at iter={}'.format(iters))
            with torch.cuda.device(args.local_rank):
                torch.save(watcher.best_tracker.model.state_dict(), '{}_iter={}.pt'.format(args.model_name, iters))
                torch.save([iters, watcher.best_tracker.opt.state_dict()], '{}_iter={}.pt.states'.format(args.model_name, iters))

        # --- validation --- #
        if check(args.eval_every) and (not args.no_valid): # and (args.local_rank == 0):

            watcher.close_progress_bar()

            with torch.no_grad():
                outputs_data = [valid_model(args, watcher, model, d, print_out=True, dataflow=['src', 'trg']) for d in dev]

            if args.tensorboard and (not args.debug):
                for outputs in outputs_data:
                    for name, value in outputs['tb_data']:
                        watcher.add_tensorboard(name, value, iters)

            if not args.debug:
                if len(outputs_data) == 1: # single pair MT
                    corpus_bleu = outputs_data[0]['corpus_bleu']
                else:
                    # for multilingual training, we use the average of all languages.
                    corpus_bleu = np.exp(np.mean([np.log(outputs['corpus_bleu'] + TINY) for outputs in outputs_data]))
                watcher.acc_best_tracker(iters, corpus_bleu)

                if args.local_rank == 0:
                    watcher.info('the best model is achieved at {}, corpus BLEU={}'.format(watcher.best_tracker.i, watcher.best_tracker.corpus_bleu))
                    
                    if watcher.best_tracker.i > best_i:
                        best_i = watcher.best_tracker.i

                        # # output the best translation for record #
                        # if decoding_path is not None:
                        #     handles = [open(os.path.join(decoding_path, name), 'w') for name in names]
                        #     for s, t, d in sorted(zip(outputs_data['src'], outputs_data['trg'], outputs_data['dec']), key=lambda a: a[0]):
                        #         print(s, file=handles[0], flush=True)l
                        #         print(t, file=handles[1], flush=True)
                        #         print(d, file=handles[2], flush=True)
                        #     for handle in handles:
                        #         handle.close()
            
            watcher.info('model:' + args.prefix + args.hp_str)

            # ---set-up a new progressor---
            watcher.set_progress_bar(args.eval_every)

        if maxsteps is None:
            maxsteps = args.maximum_steps

        if iters > maxsteps:
            watcher.info('reach the maximum updating steps.')
            break
        

        # --- training  --- #
        iters += 1
        model.train()

        def get_learning_rate(i, disable=False):
            if not disable:
                return min(max(1.0 / math.sqrt(args.d_model * i), 5e-5), i / (args.warmup * math.sqrt(args.d_model * args.warmup)))               
            return 0.001

        with Timer() as train_timer:
        
            opt.param_groups[0]['lr'] = get_learning_rate(iters, disable=False) # (args.model == 'AutoTransformer2'))
            opt.zero_grad()
                
            info_str = 'training step = {}, lr={:.7f}, '.format(iters, opt.param_groups[0]['lr'])
            info = defaultdict(lambda:[])
            pairs = []

            # prepare the data
            for inter_step in range(args.inter_size):

                def sample_a_training_set(train, prob):
                    if len(prob) == 0:  # not providing probability, sample dataset uniformly.
                        prob = [1 / len(train) for _ in train]

                    train_idx = np.random.choice(np.arange(len(train)), p=prob)
                    return next(train[train_idx])

                if len(train) == 1:  # single-pair MT:
                    batch = next(train[0])  # load the next batch of training data.
                else:
                    batch = sample_a_training_set(train, args.sample_prob)

                # --- attention visualization --- #
                if (check(args.att_plot_every, 1) and (inter_step == 0) and (args.local_rank == 0)):
                    model.module.attention_flag = True

                info_ = model(batch, dataflow=['src', 'trg'])
                info_['loss'] = info_['loss'] / args.inter_size
                info_['loss'].backward()

                pairs.append(batch.dataset.task)
                for t in info_:
                    info[t] += [info_[t].item()]
                
            # multiple steps, one update
            opt.step()

            if args.distributed:  # gather information from other workers.
                gather_dict(info)
            
            for t in info:
                if t == 'max_att':
                    info[t] = max(info[t])
                else:
                    info[t] = sum(info[t])

        info_str += '#token={}, #sentence={}, #maxtt={}, speed={} t/s | {} | '.format(
                    format(info['tokens'], 'k'), int(info['sents']), format(info['max_att'], 'm'),
                    format(info['tokens'] / train_timer.elapsed_secs, 'k'), '/'.join(pairs))

        for keyword in info:
            if keyword[:2] == 'L@':
                info_str += '{}={:.3f}, '.format(keyword, info[keyword] / args.world_size / args.inter_size)
                if args.tensorboard and (not args.debug):
                    watcher.add_tensorboard('train/{}'.format(keyword), info[keyword] / args.world_size / args.inter_size, iters)
                    
                    # -- attention visualization -- #
                    if (model.module.attention_maps is not None) and (args.local_rank == 0):
                        watcher.info('Attention visualization at Tensorboard')
                        with Timer() as visualization_timer:
                            for name, value in model.module.attention_maps:
                                watcher.add_tensorboard(name, value, iters, 'figure')
                            model.module.attention_maps = None
                        watcher.info('Attention visualization cost: {}s'.format(visualization_timer.elapsed_secs))

        watcher.step_progress_bar(info_str=info_str)

def train_autoencoder(args, watcher, model, train, dev, save_path=None, maxsteps=None):
    """
    Not yet full functionality
    """

    # optimizer for auto-encoder
    opt_all = [torch.optim.Adam(param, betas=(0.9, 0.98), eps=1e-9) for param in model.module.trainable_parameters()]
    opt = opt_all[0]

    # if resume training
    if (args.load_from != 'none') and (args.resume):
        with torch.cuda.device(args.local_rank):   # very important.
            offset, opt_states = torch.load(args.workspace_prefix + '/models/' + args.load_from + '.pt.states',
                                            map_location=lambda storage, loc: storage.cuda())
            opt.load_state_dict(opt_states)
    else:
        offset = 0
    
    iters = offset
    best_i = 0

    # confirm the saving path
    if save_path is None:
        save_path = args.model_name

    # setup a watcher
    param_to_watch = ['corpus_bleu', 'corpus_bleu_src', 'corpus_bleu_trg']
    watcher.set_progress_bar(args.eval_every)
    watcher.set_best_tracker(model, opt, save_path, args.local_rank, *param_to_watch)
    if args.tensorboard and (not args.debug):
        watcher.set_tensorboard('{}/runs/{}'.format(args.workspace_prefix, args.prefix+args.hp_str))
    
    train = iter(train)

    while True:

        # --- saving --- #
        if (iters % args.save_every == 0) and (args.local_rank == 0): # saving only works for local-rank=0
            watcher.info('save (back-up) checkpoints at iter={}'.format(iters))
            with torch.cuda.device(args.local_rank):
                torch.save(watcher.best_tracker.model.state_dict(), '{}_iter={}.pt'.format(args.model_name, iters))
                torch.save([iters, watcher.best_tracker.opt.state_dict()], '{}_iter={}.pt.states'.format(args.model_name, iters))

        # --- validation --- #
        if (iters % args.eval_every == 0) and (not args.no_valid): # and (args.local_rank == 0):

            watcher.close_progress_bar()

            with torch.no_grad():
                outputs_data_src = valid_model(args, watcher, model, dev, print_out=True, dataflow=['src', 'src'])
                outputs_data_trg = valid_model(args, watcher, model, dev, print_out=True, dataflow=['trg', 'trg'])

            if args.tensorboard and (not args.debug):
                if len(outputs_data_src['tb_data']) > 0:
                    for name, value in outputs_data_src['tb_data']:
                        watcher.add_tensorboard(name + '_s2s', value, iters)
                if len(outputs_data_trg['tb_data']) > 0:
                    for name, value in outputs_data_trg['tb_data']:
                        watcher.add_tensorboard(name + '_t2t', value, iters)

            if not args.debug:
                corpus_bleu_src = outputs_data_src['corpus_bleu']
                corpus_bleu_trg = outputs_data_trg['corpus_bleu']
                watcher.acc_best_tracker(iters, (corpus_bleu_src * corpus_bleu_trg) ** 0.5, corpus_bleu_src, corpus_bleu_trg)

                if args.local_rank == 0:
                    watcher.info('the best model is achieved at {}, corpus (AVG) BLEU={}/{}/{}'.format(
                                watcher.best_tracker.i, 
                                watcher.best_tracker.corpus_bleu, 
                                watcher.best_tracker.corpus_bleu_src,
                                watcher.best_tracker.corpus_bleu_trg))
                    
                    if watcher.best_tracker.i > best_i:
                        best_i = watcher.best_tracker.i

            watcher.info('model:' + args.prefix + args.hp_str)

            # ---set-up a new progressor---
            watcher.set_progress_bar(args.eval_every)

        if maxsteps is None:
            maxsteps = args.maximum_steps

        if iters > maxsteps:
            watcher.info('reach the maximum updating steps.')
            break
        

        # --- training  --- #
        iters += 1
        model.train()

        def get_learning_rate(i, disable=False):
            if not disable:
                return min(max(1.0 / math.sqrt(args.d_model * i), 5e-5), i / (args.warmup * math.sqrt(args.d_model * args.warmup)))               
            return 0.001

        with Timer() as train_timer:
        
            opt.param_groups[0]['lr'] = get_learning_rate(iters)
            opt.zero_grad()
                
            info_str = 'training step = {}, lr={:.7f}, '.format(iters, opt.param_groups[0]['lr'])
            info = defaultdict(lambda:[])

            # prepare the data
            for inter_step in range(args.inter_size):
                
                dataflow = ['src', 'src'] if (iters * args.inter_size  + inter_step) % 2 == 0 else ['trg', 'trg']
                batch = next(train)  # load the next batch of training data:= training both auto-encoder jointly := ? #
                
                info_ = model(batch, dataflow=dataflow, noise_level=args.input_noise)
                info_['loss'] = info_['loss'] / args.inter_size
                info_['loss'].backward()

                for t in info_:
                    info[t] += [info_[t].item()]
                
            # multiple steps, one update
            opt.step()

            if args.distributed:  # gather information from other workers.
                gather_dict(info)
            
            for t in info:
                info[t] = sum(info[t])

        info_str += '{} tokens / batch, {} tokens / sec, '.format(
                    int(info['tokens']), int(info['tokens'] / train_timer.elapsed_secs))

        for keyword in info:
            if keyword[:2] == 'L@':
                info_str += '{}={:.3f}, '.format(keyword, info[keyword] / args.world_size / args.inter_size)
                if args.tensorboard and (not args.debug):
                    watcher.add_tensorboard('train/{}'.format(keyword), info[keyword] / args.world_size / args.inter_size, iters)
        
        watcher.step_progress_bar(info_str=info_str)
