import argparse
import json
import torch
import torch.multiprocessing as mp
import torch.distributed as dist
import sys
import os
import toml
import warnings
import datetime
warnings.filterwarnings('ignore')
os.chdir(r"D:\MARONE\WOLOF\SPEECH_TO_TEXT")
# to add the path of the different on module
sys.path.append(r'CODES\WHISPER')

from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader
from time import gmtime, strftime
from utils.utils import *
from utils.metric import Metric
from dataloader.dataset import DefaultCollate
from transformers import WhisperFeatureExtractor, WhisperTokenizer, WhisperProcessor, WhisperForConditionalGeneration,  WhisperModel

def setup(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '4444'

    # initialize the process group
    dist.init_process_group("gloo", rank=rank, world_size=world_size, timeout=datetime.timedelta(seconds=3600 * 5))

def cleanup():
    dist.destroy_process_group()


def main(rank, world_size, config, resume, preload):
    os.environ['CUDA_VISIBLE_DEVICES']=config["meta"]["device_ids"]
    os.environ['TORCH_DISTRIBUTED_DEBUG'] = 'INFO'
    setup(rank, world_size)

    pretrained_path = config["meta"]["pretrained_path"]
    epochs = config["meta"]["epochs"]
    gradient_accumulation_steps = config["meta"]["gradient_accumulation_steps"]
    use_amp = config["meta"]["use_amp"]
    max_clip_grad_norm = config["meta"]["max_clip_grad_norm"]
    save_dir =  os.path.join(config["meta"]["save_dir"], config["meta"]['name'] + '/checkpoints')
    log_dir = os.path.join(config["meta"]["save_dir"], config["meta"]['name'] + '/log_dir')
    custom_tokenizer_path = os.path.join(config["meta"]["save_dir"], 'custom_tokenizer')
    tokenizer_path = os.path.join(config["meta"]["save_dir"], config["meta"]['name'],  "tokenizer")
    pretrained_tokenizer_path = os.path.join(config["meta"]["save_dir"], config["meta"]['name'],  "pretrained_tokenizer")
    
    if rank == 0:
        # Creatr dirs
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        os.makedirs(tokenizer_path, exist_ok=True)
            
            
        # Store config file
        config_name = strftime("%Y-%m-%d %H~%M~%S", gmtime()).replace(' ', '_') + '.toml'
        with open(os.path.join(config["meta"]["save_dir"], config["meta"]['name'] + '\\' + config_name), 'w+') as f:
            toml.dump(config, f)
            f.close()

    # This should be needed to be reproducible https://discuss.pytorch.org/t/setting-seed-in-torch-ddp/126638
    config["meta"]["seed"] += rank
    set_seed(config["meta"]["seed"])
    config['val_dataset']['args']['sr'] = config['meta']['sr']
    config['train_dataset']['args']['sr'] = config['meta']['sr']

    config['train_dataset']['args']['rank'] = rank
    config['val_dataset']['args']['rank'] = rank

    config["train_dataset"]["args"]["dist"] = dist
    config["val_dataset"]["args"]["dist"] = dist
    
    feature_extractor = WhisperFeatureExtractor.from_pretrained(pretrained_path, task="transcribe")
    
    config["train_dataset"]["args"]["feature_extractor"] = feature_extractor
    config["val_dataset"]["args"]["feature_extractor"] = feature_extractor

    train_base_ds = initialize_module(config["train_dataset"]["path"], args=config["train_dataset"]["args"])
    dist.barrier()
    
    
    # Initialize tokenizers
    # custom_tokenizer =  WhisperTokenizer(vocab_file=os.path.join(custom_tokenizer_path,  "vocab.json"), merges_file=os.path.join(custom_tokenizer_path,  "merges.txt"))
    tokenizer = WhisperTokenizer.from_pretrained(pretrained_path, task="transcribe")
    
    os.makedirs(pretrained_tokenizer_path, exist_ok=True)
    tokenizer.save_pretrained(pretrained_tokenizer_path)

    # check if the new tokens are already in the vocabulary
    # new_tokens = set(custom_tokenizer.get_vocab().keys()) - set(tokenizer.get_vocab().keys())

    # add the tokens to the tokenizer vocabulary
    # tokenizer.add_tokens(list(new_tokens))
    # tokenizer.add_special_tokens({'pad_token': '[PAD]'})
    
    # special_tokens = ["<|endoftext|>", "<|startoftranscript|>",  "<|en|>", '[PAD]']
    # vocab =  tokenizer.get_vocab()
    # vocab_to_id = set(vocab.keys()) - (set(custom_tokenizer.get_vocab().keys()) | set(special_tokens))

    # custom_suppressed_tokens = [vocab[key] for key in vocab_to_id]
    
    # Create processor
    processor = WhisperProcessor(feature_extractor=feature_extractor, tokenizer=tokenizer)
    tokenizer.save_pretrained(tokenizer_path)
    
    # Create the collator
    default_collate = DefaultCollate(processor, config['meta']['sr'])

    # Create train dataloader
    train_ds = train_base_ds.get_data()
    train_sampler = torch.utils.data.distributed.DistributedSampler(
        train_ds,
        num_replicas=world_size,
        rank=rank,
        **config["train_dataset"]["sampler"]
    )
    train_dl = DataLoader(
        dataset=train_ds,
        **config["train_dataset"]["dataloader"],
        sampler = train_sampler,
        collate_fn=default_collate
    )

    # Create val dataloader
    val_base_ds = initialize_module(config["val_dataset"]["path"], args=config["val_dataset"]["args"])
    val_ds = val_base_ds.get_data()
    val_sampler = torch.utils.data.distributed.DistributedSampler(
        val_ds,
        num_replicas=world_size,
        rank=rank,
        **config["val_dataset"]["sampler"]
    )
    val_dl = DataLoader(
        dataset=val_ds,
        **config["val_dataset"]["dataloader"],
        sampler = val_sampler,
        collate_fn=default_collate
    )


    model =  WhisperForConditionalGeneration.from_pretrained(pretrained_path)
    
    # add new random embeddings for the appended tokens
    # model.resize_token_embeddings(len(tokenizer)) 
    # model.config.vocab_size = tokenizer.vocab
    # Suppress tokens
    # model.config.suppress_tokens = custom_suppressed_tokens
    model.config.forced_decoder_ids = None
    model.config.suppress_tokens = []
    
    # DDP for multi-processing
    model = DDP(model.to(rank), device_ids=[rank], find_unused_parameters = False)

    # Set up metric, scheduler, optmizer
    compute_metric = Metric(tokenizer)
    optimizer = torch.optim.AdamW(
        params = model.parameters(),
        lr = config["optimizer"]["lr"]
    )
    steps_per_epoch = (len(train_dl)//gradient_accumulation_steps) + (len(train_dl)%gradient_accumulation_steps != 0)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, 
        max_lr=config["scheduler"]["max_lr"], 
        epochs=epochs, 
        steps_per_epoch = steps_per_epoch)


    if rank == 0:
        print("Number of training utterances: ", len(train_ds))
        print("Number of validation utterances: ", len(val_ds))

    trainer_class = initialize_module(config["trainer"]["path"], initialize=False)
    trainer = trainer_class(
        dist = dist,
        rank = rank,
        n_gpus = world_size,
        config = config,
        resume = resume,
        preload = preload,
        epochs = epochs,
        steps_per_epoch = steps_per_epoch,
        model = model,
        compute_metric = compute_metric,
        processor = processor,
        train_dl = train_dl,
        val_dl = val_dl,
        train_sampler = train_sampler,
        val_sampler = val_sampler,
        optimizer = optimizer,
        scheduler = scheduler,
        save_dir = save_dir,
        log_dir = log_dir,
        gradient_accumulation_steps = gradient_accumulation_steps,
        use_amp = use_amp,
        max_clip_grad_norm = max_clip_grad_norm
    )
    trainer.train()


    cleanup()

if __name__ == '__main__':
    args = argparse.ArgumentParser(description='ASR TRAIN ARGS')
    args.add_argument('-c', '--config', required=True, type=str,
                      help='config file path (default: None)')
    args.add_argument('-r', '--resume', action="store_true",
                      help='path to latest checkpoint (default: None)')
    args.add_argument('-p', '--preload', default=None, type=str,
                      help='Path to pretrained Model')            
    
    args = args.parse_args()
    config = toml.load(args.config)
    n_gpus = len(config['meta']["device_ids"].split(','))
    
    mp.spawn(
        main,
        args = (n_gpus, config, args.resume, args.preload),
        nprocs = n_gpus,
        join = True
    )

