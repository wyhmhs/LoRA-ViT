import argparse
from cgi import test
import logging
from torchvision import models
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.cuda.amp.autocast_mode import autocast
from torch.cuda.amp.grad_scaler import GradScaler
from torch.optim.lr_scheduler import CosineAnnealingLR
from tqdm import tqdm

import timm
from lora import LoRA_ViT_timm
from utils.dataloader_oai import kneeDataloader
from utils.dataloader_cxr_cn import cxrDataloader
from utils.dataloader_blood_cell import BloodDataloader
from utils.dataloader_nih import nihDataloader
from utils.result import ResultCLS
from utils.utils import init, save


def train(epoch,trainset):
    running_loss = 0.0
    this_lr = scheduler.get_last_lr()[0]
    net.train()
    for image, label in tqdm(trainset, ncols=60, desc="train", unit="b", leave=None):
        image, label = image.to(device), label.to(device)
        optimizer.zero_grad()
        with autocast(enabled=True):
            pred = net.forward(image)
            loss = loss_func(pred, label)
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        running_loss = running_loss + loss.item()
    scheduler.step()

    loss = running_loss / len(trainset)
    logging.info(f"\n\nEPOCH: {epoch}, LOSS : {loss:.3f}, LR: {this_lr:.2e}")
    return


@torch.no_grad()
def eval(epoch,testset,datatype='val'):
    result.init()
    net.eval()
    for image, label in tqdm(testset, ncols=60, desc=datatype, unit="b", leave=None):
        image, label = image.to(device), label.to(device)
        with autocast(enabled=True):
            pred = net.forward(image)
            result.eval(label, pred)
    result.print(epoch,datatype)
    return


if __name__ == "__main__":
    scaler = GradScaler()
    parser = argparse.ArgumentParser()
    parser.add_argument("-bs", type=int, default=16)
    parser.add_argument("-fold", type=int, default=0)
    parser.add_argument("-data_path",type=str, default='../data/NIH_X-ray/')
    parser.add_argument("-data_info",type=str,default='nih_split_712.json')
    parser.add_argument("-annotation",type=str,default='Data_Entry_2017_jpg.csv')
    parser.add_argument("-lr", type=float, default=1e-3)
    parser.add_argument("-epochs", type=int, default=20)
    parser.add_argument("-num_workers", type=int, default=4)
    parser.add_argument("-num_classes", "-nc", type=int, default=14)
    parser.add_argument("-train_type", "-tt", type=str, default="linear", help="lora: only train lora, full: finetune on all, linear: finetune only on linear layer")
    parser.add_argument("-rank", "-r", type=int, default=4)
    parser.add_argument("-vit", type=str, default="base")
    cfg = parser.parse_args()
    ckpt_path = init()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logging.info(cfg)

    #   a.根据local_rank来设定当前使用哪块GPU
    # torch.cuda.set_device(local_rank)
    #   b.初始化DDP，使用默认backend(nccl)就行。如果是CPU模型运行，需要选择其他后端。
    # dist.init_process_group(backend='nccl')
    if cfg.train_type=='resnet50':
        model=models.__dict__[cfg.train_type]()
        model.load_state_dict(torch.load('../preTrain/resnet50-19c8e357.pth'))

        # model.load_state_dict()
    else:
        if cfg.vit == "base":
            model = timm.create_model("vit_base_patch16_224", pretrained=False, checkpoint_path="../preTrain/B_16-i21k-300ep-lr_0.001-aug_medium1-wd_0.1-do_0.0-sd_0.0--imagenet2012-steps_20k-lr_0.01-res_224.npz")
        elif cfg.vit == "small":
            model = timm.create_model("vit_small_patch16_224", pretrained=False, checkpoint_path="../preTrain/S_16-i21k-300ep-lr_0.001-aug_light1-wd_0.03-do_0.0-sd_0.0--imagenet2012-steps_20k-lr_0.03-res_224.npz")
        elif cfg.vit == "large":
            model = timm.create_model("vit_large_patch16_224", pretrained=False, checkpoint_path="../preTrain/L_16-i21k-300ep-lr_0.001-aug_medium1-wd_0.1-do_0.1-sd_0.1--imagenet2012-steps_20k-lr_0.01-res_224.npz")
        else:
            print("Wrong training type")
            exit()
        
    if cfg.train_type == "lora":
        lora_model = LoRA_ViT_timm(model, r=cfg.rank, dim=768, num_classes=cfg.num_classes)
        num_params = sum(p.numel() for p in lora_model.parameters() if p.requires_grad)
        print(f"trainable parameters: {num_params/2**20:.3f}M")
        net = lora_model.to(device)
    elif cfg.train_type == "full":
        model.fc = nn.Linear(768, cfg.num_classes)
        num_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
        print(f"trainable parameters: {num_params/2**20:.3f}M")
        net = model.to(device)
    elif cfg.train_type == "linear":
        model.fc = nn.Linear(768, cfg.num_classes)
        for param in model.parameters():
            param.requires_grad = False
        for param in model.fc.parameters():
            param.requires_grad = True
        num_params = sum(p.numel() for p in model.fc.parameters())
        print(f"trainable parameters: {num_params/2**20:.3f}M")
        net = model.to(device)
    elif cfg.train_type=='resnet50':
        infeature = model.fc.in_features
        model.fc = nn.Linear(infeature, cfg.num_classes)
        num_params = sum(p.numel() for p in model.fc.parameters())
        print(f"trainable parameters: {num_params/2**20:.3f}M")
        net = model.to(device)
    else:
        print("Wrong training type")
        exit()
    net = torch.nn.DataParallel(net)
    if cfg.data_path == "OAI-train":
        trainset, valset, testset = kneeDataloader(cfg)
    elif cfg.data_path == "ChinaSet_AllFiles":
        trainset, valset, testset = cxrDataloader(cfg)
    elif cfg.data_path == "blood-cells":
        trainset, valset, testset = BloodDataloader(cfg)
    loss_func = nn.CrossEntropyLoss(label_smoothing=0.1).to(device)
    # trainset,valset, testset=nihDataloader(cfg)
    # loss_func = nn.BCEWithLogitsLoss().to(device)
    optimizer = optim.Adam(net.parameters(), lr=cfg.lr)
    scheduler = CosineAnnealingLR(optimizer, cfg.epochs, 1e-6)
    result = ResultCLS(cfg.num_classes)

    for epoch in range(1, cfg.epochs+1):
        train(epoch,trainset)
        if epoch%1==0:
            eval(epoch,valset,datatype='val')
            if result.best_epoch == result.epoch:
                torch.save(net.state_dict(), ckpt_path.replace(".pt", "_best.pt"))
                eval(epoch,testset,datatype='test')
                logging.info(f"BEST VAL: {result.best_val_result:.3f}, TEST: {result.test_auc:.3f}, EPOCH: {(result.best_epoch):3}")
                # logging.info(result.test_mls_auc)
