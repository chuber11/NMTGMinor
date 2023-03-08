#!/usr/bin/env python
from __future__ import division

import onmt
import onmt.markdown
import onmt.modules
import argparse
import torch
import time, datetime
from onmt.data.mmap_indexed_dataset import MMapIndexedDataset
from onmt.data.scp_dataset import SCPIndexDataset
from onmt.data.wav_dataset import WavDataset
from onmt.modules.loss import NMTLossFunc, NMTAndCTCLossFunc
from options import make_parser
from collections import defaultdict
from onmt.constants import add_tokenidx
import os
import numpy as np

parser = argparse.ArgumentParser(description='train_distributed.py')
onmt.markdown.add_md_help_argument(parser)

# Please look at the options file to see the options regarding models and data
parser = make_parser(parser)

opt = parser.parse_args()

# An ugly hack to have weight norm on / off
onmt.constants.weight_norm = opt.weight_norm
onmt.constants.checkpointing = opt.checkpointing
onmt.constants.max_position_length = opt.max_position_length

# Use static dropout if checkpointing > 0
if opt.checkpointing > 0:
    onmt.constants.static = True

if torch.cuda.is_available() and not opt.gpus:
    print("WARNING: You have a CUDA device, should run with -gpus 0")

torch.manual_seed(opt.seed)


def numpy_to_torch(tensor_list):

    out_list = list()

    for tensor in tensor_list:
        if isinstance(tensor, np.ndarray):
            out_list.append(torch.from_numpy(tensor))
        else:
            out_list.append(tensor)

    return out_list


def run_process(gpu, train_data, valid_data, dicts, opt, checkpoint):

    # from onmt.train_utils.mp_trainer import Trainer
    from onmt.train_utils.classify_trainer import ClassifierTrainer

    trainer = ClassifierTrainer(gpu, train_data, valid_data, dicts, opt)
    trainer.run(checkpoint=checkpoint)


def main():
    if not opt.multi_dataset:
        if opt.data_format in ['bin', 'raw']:
            start = time.time()

            if opt.data.endswith(".train.pt"):
                print("Loading data from '%s'" % opt.data)
                dataset = torch.load(opt.data)
            else:
                print("Loading data from %s" % opt.data + ".train.pt")
                dataset = torch.load(opt.data + ".train.pt")

            elapse = str(datetime.timedelta(seconds=int(time.time() - start)))
            print("Done after %s" % elapse)

            dicts = dataset['dicts']
            onmt.constants = add_tokenidx(opt, onmt.constants, dicts)

            # For backward compatibility
            train_dict = defaultdict(lambda: None, dataset['train'])
            valid_dict = defaultdict(lambda: None, dataset['valid'])

            if train_dict['src_lang'] is not None:
                assert 'langs' in dicts
                train_src_langs = train_dict['src_lang']
                train_tgt_langs = train_dict['tgt_lang']
            else:
                # allocate new languages
                dicts['langs'] = {'src': 0, 'tgt': 1}
                train_src_langs = list()
                train_tgt_langs = list()
                # Allocation one for the bilingual case
                train_src_langs.append(torch.Tensor([dicts['langs']['src']]))
                train_tgt_langs.append(torch.Tensor([dicts['langs']['tgt']]))

            train_data = onmt.Dataset(numpy_to_torch(train_dict['src']), numpy_to_torch(train_dict['tgt']),
                                      train_dict['src_sizes'], train_dict['tgt_sizes'],
                                      train_src_langs, train_tgt_langs,
                                      batch_size_words=opt.batch_size_words,
                                      data_type=dataset.get("type", "text"), sorting=True,
                                      batch_size_sents=opt.batch_size_sents,
                                      multiplier=opt.batch_size_multiplier,
                                      augment=opt.augment_speech, sa_f=opt.sa_f, sa_t=opt.sa_t,
                                      upsampling=opt.upsampling,
                                      num_split=1)

            if valid_dict['src_lang'] is not None:
                assert 'langs' in dicts
                valid_src_langs = valid_dict['src_lang']
                valid_tgt_langs = valid_dict['tgt_lang']
            else:
                # allocate new languages
                valid_src_langs = list()
                valid_tgt_langs = list()

                # Allocation one for the bilingual case
                valid_src_langs.append(torch.Tensor([dicts['langs']['src']]))
                valid_tgt_langs.append(torch.Tensor([dicts['langs']['tgt']]))

            valid_data = onmt.Dataset(numpy_to_torch(valid_dict['src']), numpy_to_torch(valid_dict['tgt']),
                                      valid_dict['src_sizes'], valid_dict['tgt_sizes'],
                                      valid_src_langs, valid_tgt_langs,
                                      batch_size_words=opt.batch_size_words,
                                      data_type=dataset.get("type", "text"), sorting=True,
                                      batch_size_sents=opt.batch_size_sents,
                                      multiplier=opt.batch_size_multiplier,
                                      cleaning=True,
                                      upsampling=opt.upsampling)

            print(' * number of training sentences. %d' % len(dataset['train']['src']))
            print(' * maximum batch size (words per batch). %d' % opt.batch_size_words)

        # Loading asr data structures
        elif opt.data_format in ['scp', 'scpmem', 'mmem', 'wav']:
            print("Loading memory mapped data files ....")
            start = time.time()
            from onmt.data.mmap_indexed_dataset import MMapIndexedDataset
            from onmt.data.scp_dataset import SCPIndexDataset

            dicts = torch.load(opt.data + ".dict.pt")
            # onmt.constants = add_tokenidx(opt, onmt.constants, dicts)

            if opt.data_format in ['scp', 'scpmem']:
                audio_data = torch.load(opt.data + ".scp_path.pt")
            elif opt.data_format in ['wav']:
                audio_data = torch.load(opt.data + ".wav_path.pt")

            # allocate languages if not
            if 'langs' not in dicts:
                dicts['langs'] = {'src': 0, 'tgt': 1}
            else:
                print(dicts['langs'])

            train_path = opt.data + '.train'
            if opt.data_format in ['scp', 'scpmem']:
                train_src = SCPIndexDataset(audio_data['train'], concat=opt.concat)
                if 'train_past' in audio_data:
                    past_train_src = SCPIndexDataset(audio_data['train_past'],
                                                     concat=opt.concat, shared_object=train_src)
                else:
                    past_train_src = None
            elif opt.data_format in ['wav']:
                train_src = WavDataset(audio_data['train'])
                past_train_src = None
            else:
                train_src = MMapIndexedDataset(train_path + '.src')
                past_train_src = None

            train_tgt = MMapIndexedDataset(train_path + '.tgt')

            # check the lang files if they exist (in the case of multi-lingual models)
            if os.path.exists(train_path + '.src_lang.bin'):
                assert 'langs' in dicts
                train_src_langs = MMapIndexedDataset(train_path + '.src_lang')
                train_tgt_langs = MMapIndexedDataset(train_path + '.tgt_lang')
            else:
                train_src_langs = list()
                train_tgt_langs = list()
                # Allocate a Tensor(1) for the bilingual case
                train_src_langs.append(torch.Tensor([dicts['langs']['src']]))
                train_tgt_langs.append(torch.Tensor([dicts['langs']['tgt']]))

            # check the length files if they exist
            if os.path.exists(train_path + '.src_sizes.npy'):
                train_src_sizes = np.load(train_path + '.src_sizes.npy')
                train_tgt_sizes = np.load(train_path + '.tgt_sizes.npy')
            else:
                train_src_sizes, train_tgt_sizes = None, None

            # check the length files if they exist
            if os.path.exists(train_path + '.past_src_sizes.npy'):
                past_train_src_sizes = np.load(train_path + '.past_src_sizes.npy')
            else:
                past_train_src_sizes = None

            if opt.data_format in ['scp', 'scpmem']:
                data_type = 'audio'
            elif opt.data_format in ['wav']:
                data_type = 'wav'
            else:
                data_type = 'text'

            train_data = onmt.Dataset(train_src,
                                      train_tgt,
                                      train_src_sizes, train_tgt_sizes,
                                      train_src_langs, train_tgt_langs,
                                      batch_size_words=opt.batch_size_words,
                                      data_type=data_type, sorting=True,
                                      batch_size_sents=opt.batch_size_sents,
                                      multiplier=opt.batch_size_multiplier,
                                      augment=opt.augment_speech, sa_f=opt.sa_f, sa_t=opt.sa_t,
                                      cleaning=True, verbose=True,
                                      input_size=opt.input_size,
                                      past_src_data=past_train_src,
                                      min_src_len=0, min_tgt_len=0,
                                      past_src_data_sizes=past_train_src_sizes,
                                      constants=onmt.constants)

            valid_path = opt.data + '.valid'
            if opt.data_format in ['scp', 'scpmem']:
                valid_src = SCPIndexDataset(audio_data['valid'], concat=opt.concat)
                if 'valid_past' in audio_data:
                    past_valid_src = SCPIndexDataset(audio_data['valid_past'],
                                                     concat=opt.concat, shared_object=valid_src)
                else:
                    past_valid_src = None
            elif opt.data_format in ['wav']:
                valid_src = WavDataset(audio_data['valid'])
                past_valid_src = None
            else:
                valid_src = MMapIndexedDataset(valid_path + '.src')
                past_valid_src = None

            valid_tgt = MMapIndexedDataset(valid_path + '.tgt')

            if os.path.exists(valid_path + '.src_lang.bin'):
                assert 'langs' in dicts
                valid_src_langs = MMapIndexedDataset(valid_path + '.src_lang')
                valid_tgt_langs = MMapIndexedDataset(valid_path + '.tgt_lang')
            else:
                valid_src_langs = list()
                valid_tgt_langs = list()

                # Allocation one for the bilingual case
                valid_src_langs.append(torch.Tensor([dicts['langs']['src']]))
                valid_tgt_langs.append(torch.Tensor([dicts['langs']['tgt']]))

            # check the length files if they exist
            if os.path.exists(valid_path + '.src_sizes.npy'):
                valid_src_sizes = np.load(valid_path + '.src_sizes.npy')
                valid_tgt_sizes = np.load(valid_path + '.tgt_sizes.npy')
            else:
                valid_src_sizes, valid_tgt_sizes = None, None

            # check the length files if they exist
            if os.path.exists(valid_path + '.past_src_sizes.npy'):
                past_valid_src_sizes = np.load(valid_path + '.past_src_sizes.npy')
            else:
                past_valid_src_sizes = None

            # we can use x2 batch eize for validation 
            valid_data = onmt.Dataset(valid_src, valid_tgt,
                                      valid_src_sizes, valid_tgt_sizes,
                                      valid_src_langs, valid_tgt_langs,
                                      batch_size_words=opt.batch_size_words * 2,
                                      multiplier=opt.batch_size_multiplier,
                                      data_type=data_type, sorting=True,
                                      input_size=opt.input_size,
                                      batch_size_sents=opt.batch_size_sents,
                                      cleaning=True, verbose=True, debug=True,
                                      past_src_data=past_valid_src,
                                      past_src_data_sizes=past_valid_src_sizes,
                                      min_src_len=0, min_tgt_len=0,
                                      constants=onmt.constants)

            elapse = str(datetime.timedelta(seconds=int(time.time() - start)))
            print("Done after %s" % elapse)

        else:
            raise NotImplementedError

        print(' * number of sentences in training data: %d' % train_data.size())
        print(' * number of sentences in validation data: %d' % valid_data.size())

    else:
        print("[INFO] Reading multiple dataset ...")
        # raise NotImplementedError

        dicts = torch.load(opt.data + ".dict.pt")
        # onmt.constants = add_tokenidx(opt, onmt.constants, dicts)

        root_dir = os.path.dirname(opt.data)

        print("Loading training data ...")

        train_dirs, valid_dirs = dict(), dict()

        # scan the data directory to find the training data
        for dir_ in os.listdir(root_dir):
            if os.path.isdir(os.path.join(root_dir, dir_)):
                if str(dir_).startswith("train"):
                    idx = int(dir_.split(".")[1])
                    train_dirs[idx] = dir_
                if dir_.startswith("valid"):
                    idx = int(dir_.split(".")[1])
                    valid_dirs[idx] = dir_

        train_sets, valid_sets = list(), list()

        for (idx_, dir_) in sorted(train_dirs.items()):

            data_dir = os.path.join(root_dir, dir_)
            print("[INFO] Loading training data %i from %s" % (idx_, dir_))

            if opt.data_format in ['bin', 'raw']:
                raise NotImplementedError

            elif opt.data_format in ['scp', 'scpmem', 'mmem', 'wav']:
                from onmt.data.mmap_indexed_dataset import MMapIndexedDataset
                from onmt.data.scp_dataset import SCPIndexDataset

                if opt.data_format in ['scp', 'scpmem']:
                    audio_data = torch.load(os.path.join(data_dir, "data.scp_path.pt"))
                    src_data = SCPIndexDataset(audio_data, concat=opt.concat)
                elif opt.data_format in ['wav']:
                    audio_data = torch.load(os.path.join(data_dir, "data.scp_path.pt"))
                    src_data = WavDataset(audio_data)
                else:
                    src_data = MMapIndexedDataset(os.path.join(data_dir, "data.src"))

                tgt_data = MMapIndexedDataset(os.path.join(data_dir, "data.tgt"))

                src_lang_data = MMapIndexedDataset(os.path.join(data_dir, 'data.src_lang'))
                tgt_lang_data = MMapIndexedDataset(os.path.join(data_dir, 'data.tgt_lang'))

                if os.path.exists(os.path.join(data_dir, 'data.src_sizes.npy')):
                    src_sizes = np.load(os.path.join(data_dir, 'data.src_sizes.npy'))
                    tgt_sizes = np.load(os.path.join(data_dir, 'data.tgt_sizes.npy'))
                else:
                    src_sizes, sizes = None, None

                if opt.data_format in ['scp', 'scpmem']:
                    data_type = 'audio'
                elif opt.data_format in ['wav']:
                    data_type = 'wav'
                else:
                    data_type = 'text'

                train_data = onmt.Dataset(src_data,
                                          tgt_data,
                                          src_sizes, tgt_sizes,
                                          src_lang_data, tgt_lang_data,
                                          batch_size_words=opt.batch_size_words,
                                          data_type=data_type, sorting=True,
                                          batch_size_sents=opt.batch_size_sents,
                                          multiplier=opt.batch_size_multiplier,
                                          src_align_right=opt.src_align_right,
                                          upsampling=opt.upsampling,
                                          augment=opt.augment_speech, sa_f=opt.sa_f, sa_t=opt.sa_t,
                                          cleaning=True, verbose=True,
                                          input_size=opt.input_size,
                                          constants=onmt.constants)

                train_sets.append(train_data)

        for (idx_, dir_) in sorted(valid_dirs.items()):

            data_dir = os.path.join(root_dir, dir_)

            print("[INFO] Loading validation data %i from %s" % (idx_, dir_))

            if opt.data_format in ['bin', 'raw']:
                raise NotImplementedError

            elif opt.data_format in ['scp', 'scpmem', 'mmem', 'wav']:

                if opt.data_format in ['scp', 'scpmem']:
                    audio_data = torch.load(os.path.join(data_dir, "data.scp_path.pt"))
                    src_data = SCPIndexDataset(audio_data, concat=opt.concat)
                elif opt.data_format in ['wav']:
                    audio_data = torch.load(os.path.join(data_dir, "data.scp_path.pt"))
                    src_data = WavDataset(audio_data)
                else:
                    src_data = MMapIndexedDataset(os.path.join(data_dir, "data.src"))

                tgt_data = MMapIndexedDataset(os.path.join(data_dir, "data.tgt"))

                src_lang_data = MMapIndexedDataset(os.path.join(data_dir, 'data.src_lang'))
                tgt_lang_data = MMapIndexedDataset(os.path.join(data_dir, 'data.tgt_lang'))

                if os.path.exists(os.path.join(data_dir, 'data.src_sizes.npy')):
                    src_sizes = np.load(os.path.join(data_dir, 'data.src_sizes.npy'))
                    tgt_sizes = np.load(os.path.join(data_dir, 'data.tgt_sizes.npy'))
                else:
                    src_sizes, sizes = None, None

                if opt.encoder_type == 'audio':
                    data_type = 'audio'
                else:
                    data_type = 'text'

                valid_data = onmt.Dataset(src_data, tgt_data,
                                          src_sizes, tgt_sizes,
                                          src_lang_data, tgt_lang_data,
                                          batch_size_words=opt.batch_size_words,
                                          multiplier=opt.batch_size_multiplier,
                                          data_type=data_type, sorting=True,
                                          batch_size_sents=opt.batch_size_sents,
                                          src_align_right=opt.src_align_right,
                                          min_src_len=1, min_tgt_len=3,
                                          input_size=opt.input_size,
                                          cleaning=True, verbose=True, constants=onmt.constants)

                valid_sets.append(valid_data)

        train_data = train_sets
        valid_data = valid_sets

    if opt.load_from:
        checkpoint = torch.load(opt.load_from, map_location=lambda storage, loc: storage)
        print("* Loading dictionaries from the checkpoint")
        del checkpoint['model']
        del checkpoint['optim']
        dicts = checkpoint['dicts']
    else:
        dicts['tgt'].patch(opt.patch_vocab_multiplier)
        checkpoint = None

    if "src" in dicts:
        print(' * vocabulary size. source = %d; target = %d' %
              (dicts['src'].size(), dicts['tgt'].size()))
    else:
        print(' * vocabulary size. target = %d' %
              (dicts['tgt'].size()))

    os.environ['MASTER_ADDR'] = opt.master_addr  # default 'localhost'
    os.environ['MASTER_PORT'] = opt.master_port  # default '8888'

    # spawn N processes for N gpus
    # each process has a different trainer
    if len(opt.gpus) > 1:
        torch.multiprocessing.spawn(run_process, nprocs=len(opt.gpus),
                                    args=(train_data, valid_data, dicts, opt, checkpoint))
    else:
        run_process(0, train_data, valid_data, dicts, opt, checkpoint)


if __name__ == "__main__":
    main()