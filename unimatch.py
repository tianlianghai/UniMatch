import argparse
import logging
import os
import pprint
import numpy as np

import torch
from torch import nn
import torch.backends.cudnn as cudnn
from torch.optim import SGD
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import yaml

from dataset.semi import SemiDataset
from model.semseg.deeplabv3plus import DeepLabV3Plus
from supervised import evaluate
from util.classes import CLASSES
from util.ohem import ProbOhemCrossEntropy2d
from util.utils import count_params, init_log, AverageMeter
from accelerate import Accelerator
from torch.utils.data import Subset
from tqdm.auto import tqdm
import time
from tlhengine.loss import FocalLoss
from tlhengine.utils import copy_state_dict_with_prefix

parser = argparse.ArgumentParser(description='Revisiting Weak-to-Strong Consistency in Semi-Supervised Semantic Segmentation')
# parser.add_argument('--config', type=str, required=True)
# parser.add_argument('--labeled-id-path', type=str, required=True)
# parser.add_argument('--unlabeled-id-path', type=str, required=True)
# parser.add_argument('--save-path', type=str, required=True)
parser.add_argument('--local_rank', default=0, type=int)
# parser.add_argument('--port', default=None, type=int)
parser.add_argument('--subset', action='store_true')
parser.add_argument('--eval-interval', default=1, type=int)
parser.add_argument('--resume', action='store_true')
parser.add_argument('--batch-size', type=int)
parser.add_argument('--dataset', default='pascal')
parser.add_argument('--method', default='unimatch')
parser.add_argument('--exp', type=str, default='')
parser.add_argument('--split', default='732')
parser.add_argument('--gradient-accumulation-steps', dest='gas',type=int, default=1)
parser.add_argument('--cpu', action='store_true')
parser.add_argument('--debug', action='store_true')
parser.add_argument('--number-workers', dest='nw', default=4)
parser.add_argument('--extra-ckp' )
parser.add_argument('--criterion', )
parser.add_argument('--config')
parser.add_argument('--comment', )

def main():
    args = parser.parse_args()
    if args.debug:
        original_repr = torch.Tensor.__repr__
        def custom_repr(self):
            return f'{{Tensor:{tuple(self.shape)}}} {original_repr(self)}'

        torch.Tensor.__repr__ = custom_repr
        
        # np_orig_repr = np.ndarray.__repr__
        # def np_custom_repr(self):
        #     return f'{{Array:{tuple(self.shape)}}} {np_orig_repr(self)}'
        # np.ndarray.__repr__ = np_custom_repr
        # np.random.randn()
        
    accelerator = Accelerator(mixed_precision='fp16', split_batches=False, gradient_accumulation_steps=args.gas, cpu=args.cpu)
    ngpu = accelerator.num_processes
    args.ngpu = ngpu
    args.config=f'configs/{args.dataset}.yaml' if args.config is None else args.config
    cfg = yaml.load(open(args.config, "r"), Loader=yaml.Loader)
    args.labeled_id_path=f'splits/{args.dataset}/{args.split}/labeled.txt'
    args.unlabeled_id_path=f'splits/{args.dataset}/{args.split}/unlabeled.txt'

        
    loss_name = cfg['criterion']['name']
    exp_name = '_'.join([args.exp, f'ngpu{ngpu}',f'bs{args.batch_size}', f'ga{args.gas}', f'loss{loss_name}'])
    backbone = cfg['backbone']
    args.save_path=f'exp/{args.dataset}/{args.method}/{backbone}/{args.split}/{exp_name}'
    os.makedirs(args.save_path, exist_ok=True)
    batch_size = cfg['batch_size'] if not args.batch_size else args.batch_size
    
    logger = init_log('global', logging.INFO, args.save_path)
    logger.propagate = 0
    logger_filename = logger.handlers[1].baseFilename
    print(f'logger name is {logger_filename}')
    print(f'logger base name {os.path.basename(logger_filename)}')
    
    if args.comment:
        logger.info(f'{args.comment}')
    logger_base = os.path.splitext(os.path.basename(logger_filename))[0]
    if accelerator.is_main_process:
        all_args = {**cfg, **vars(args)}
        logger.info('{}\n'.format(pprint.pformat(all_args)))
        logger.info(f'per gpu batch size is {batch_size}')
        logger.info(f'real batch size is {batch_size * ngpu}')
        writer = SummaryWriter(args.save_path, filename_suffix='_'+logger_base)
        
        os.makedirs(args.save_path, exist_ok=True)
    
    cudnn.enabled = True
    cudnn.benchmark = True

    model = DeepLabV3Plus(cfg)
    optimizer = SGD([{'params': model.backbone.parameters(), 'lr': cfg['lr']},
                    {'params': [param for name, param in model.named_parameters() if 'backbone' not in name],
                    'lr': cfg['lr'] * cfg['lr_multi']}], lr=cfg['lr'], momentum=0.9, weight_decay=1e-4)
    
    if accelerator.is_main_process:
        logger.info('Total params: {:.1f}M\n'.format(count_params(model)))
    if accelerator.num_processes > 1:
        logger.info("using sync bn")
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    else: 
        logger.info("not using sync bn")
 

    if cfg['criterion']['name'] == 'CELoss':
        criterion_l = nn.CrossEntropyLoss(**cfg['criterion']['kwargs'])
    elif cfg['criterion']['name'] == 'OHEM':
        criterion_l = ProbOhemCrossEntropy2d(**cfg['criterion']['kwargs'])
    elif cfg['criterion']['name'] == 'FocalLoss':
        criterion_l = FocalLoss(**cfg['criterion']['kwargs'])
        logger.info('using focal loss')
    else:
        raise NotImplementedError('%s criterion is not implemented' % cfg['criterion']['name'])

    if cfg['criterion']['name'] == 'FocalLoss':
        criterion_u = FocalLoss(reduction='none')
    else:
        criterion_u = nn.CrossEntropyLoss(reduction='none')


    trainset_u = SemiDataset(cfg['dataset'], cfg['data_root'], 'train_u',
                             cfg['crop_size'], args.unlabeled_id_path)
    trainset_l = SemiDataset(cfg['dataset'], cfg['data_root'], 'train_l',
                             cfg['crop_size'], args.labeled_id_path, nsample=len(trainset_u.ids))
    
    valset = SemiDataset(cfg['dataset'], cfg['data_root'], 'val')
    if args.subset:
        trainset_u = Subset(trainset_u, range(64))
        trainset_l = Subset(trainset_l, range(64))
        valset = Subset(valset, range(64))

    trainloader_l = DataLoader(trainset_l, batch_size,
                               pin_memory=True, num_workers=args.nw, drop_last=True, )
    trainloader_u = DataLoader(trainset_u, batch_size,
                               pin_memory=True, num_workers=args.nw, drop_last=True, )
    valloader = DataLoader(valset, 1, pin_memory=True, num_workers=args.nw,
                           drop_last=False, )
    model, trainloader_l, trainloader_u, optimizer, valloader = accelerator.prepare( model, trainloader_l, trainloader_u, optimizer, valloader )
    total_iters = len(trainloader_u) * cfg['epochs']
    previous_best = 0.0
    epoch = -1
    
    if os.path.exists(os.path.join(args.save_path, 'latest.pth')) and args.resume:
        ckp_path = os.path.join(args.save_path, 'latest.pth')
        checkpoint = torch.load(ckp_path)
        # model.load_state_dict(checkpoint['model'])
        copy_state_dict_with_prefix(checkpoint['model'], model, src_prefix='module.', )
        optimizer.load_state_dict(checkpoint['optimizer'])
        epoch = checkpoint['epoch']
        previous_best = checkpoint['previous_best']
        
        if accelerator.is_main_process:
            logger.info('************ Load from checkpoint at epoch %i\n' % epoch)
            
    elif args.extra_ckp:
        # if provide ckp manually, start training from 0 epoch 
        ckp_path = args.extra_ckp
        checkpoint = torch.load(ckp_path)
        logger.info(f"using extra ckp from {ckp_path}")
        copy_state_dict_with_prefix(checkpoint['model'], model, src_prefix='module.', )
        # model.load_state_dict(checkpoint['model'])
        previous_best = checkpoint['previous_best']
        
        if accelerator.is_main_process:
            logger.info('************ Load from checkpoint at epoch %i\n' % epoch)
            

            
    
    
    for epoch in range(epoch + 1, cfg['epochs']):
        if accelerator.is_main_process:
            logger.info('===========> Epoch: {:}, LR: {:.5f}, Previous best: {:.2f}'.format(
                epoch, optimizer.param_groups[0]['lr'], previous_best))

        total_loss = AverageMeter()
        total_loss_x = AverageMeter()
        total_loss_s = AverageMeter()
        total_loss_w_fp = AverageMeter()
        total_mask_ratio = AverageMeter()
        total_time_taken = AverageMeter()

        loader = zip(trainloader_l, trainloader_u, trainloader_u)
        # pbar = tqdm(loader, disable=not accelerator.is_main_process, ncols=100)
        for i, ((img_x, mask_x),
                (img_u_w, img_u_s1, img_u_s2, ignore_mask, cutmix_box1, cutmix_box2),
                (img_u_w_mix, img_u_s1_mix, img_u_s2_mix, ignore_mask_mix, _, _)) in enumerate(loader):
            if accelerator.is_main_process:
                start_batch_time = time.time()

            with torch.no_grad():
                model.eval()

                pred_u_w_mix = model(img_u_w_mix).detach()
                conf_u_w_mix = pred_u_w_mix.softmax(dim=1).max(dim=1)[0]
                mask_u_w_mix = pred_u_w_mix.argmax(dim=1)

            img_u_s1[cutmix_box1.unsqueeze(1).expand(img_u_s1.shape) == 1] = \
                img_u_s1_mix[cutmix_box1.unsqueeze(1).expand(img_u_s1.shape) == 1]
            img_u_s2[cutmix_box2.unsqueeze(1).expand(img_u_s2.shape) == 1] = \
                img_u_s2_mix[cutmix_box2.unsqueeze(1).expand(img_u_s2.shape) == 1]

            model.train()

            num_lb, num_ulb = img_x.shape[0], img_u_w.shape[0]

            preds, preds_fp = model(torch.cat((img_x, img_u_w)), True)
            pred_x, pred_u_w = preds.split([num_lb, num_ulb])
            pred_u_w_fp = preds_fp[num_lb:]

            pred_u_s1, pred_u_s2 = model(torch.cat((img_u_s1, img_u_s2))).chunk(2)

            pred_u_w = pred_u_w.detach()
            conf_u_w = pred_u_w.softmax(dim=1).max(dim=1)[0]
            mask_u_w = pred_u_w.argmax(dim=1)

            mask_u_w_cutmixed1, conf_u_w_cutmixed1, ignore_mask_cutmixed1 = \
                mask_u_w.clone(), conf_u_w.clone(), ignore_mask.clone()
            mask_u_w_cutmixed2, conf_u_w_cutmixed2, ignore_mask_cutmixed2 = \
                mask_u_w.clone(), conf_u_w.clone(), ignore_mask.clone()

            mask_u_w_cutmixed1[cutmix_box1 == 1] = mask_u_w_mix[cutmix_box1 == 1]
            conf_u_w_cutmixed1[cutmix_box1 == 1] = conf_u_w_mix[cutmix_box1 == 1]
            ignore_mask_cutmixed1[cutmix_box1 == 1] = ignore_mask_mix[cutmix_box1 == 1]

            mask_u_w_cutmixed2[cutmix_box2 == 1] = mask_u_w_mix[cutmix_box2 == 1]
            conf_u_w_cutmixed2[cutmix_box2 == 1] = conf_u_w_mix[cutmix_box2 == 1]
            ignore_mask_cutmixed2[cutmix_box2 == 1] = ignore_mask_mix[cutmix_box2 == 1]

            loss_x = criterion_l(pred_x, mask_x)

            loss_u_s1 = criterion_u(pred_u_s1, mask_u_w_cutmixed1)
            loss_u_s1 = loss_u_s1 * ((conf_u_w_cutmixed1 >= cfg['conf_thresh']) & (ignore_mask_cutmixed1 != 255))
            loss_u_s1 = loss_u_s1.sum() / (ignore_mask_cutmixed1 != 255).sum().item()

            loss_u_s2 = criterion_u(pred_u_s2, mask_u_w_cutmixed2)
            loss_u_s2 = loss_u_s2 * ((conf_u_w_cutmixed2 >= cfg['conf_thresh']) & (ignore_mask_cutmixed2 != 255))
            loss_u_s2 = loss_u_s2.sum() / (ignore_mask_cutmixed2 != 255).sum().item()

            loss_u_w_fp = criterion_u(pred_u_w_fp, mask_u_w)
            loss_u_w_fp = loss_u_w_fp * ((conf_u_w >= cfg['conf_thresh']) & (ignore_mask != 255))
            loss_u_w_fp = loss_u_w_fp.sum() / (ignore_mask != 255).sum().item()

            loss = (loss_x + loss_u_s1 * 0.25 + loss_u_s2 * 0.25 + loss_u_w_fp * 0.5) / 2.0


            optimizer.zero_grad()
            accelerator.backward(loss)
            optimizer.step()

            total_loss.update(loss.item())
            total_loss_x.update(loss_x.item())
            total_loss_s.update((loss_u_s1.item() + loss_u_s2.item()) / 2.0)
            total_loss_w_fp.update(loss_u_w_fp.item())
            
            mask_ratio = ((conf_u_w >= cfg['conf_thresh']) & (ignore_mask != 255)).sum().item() / \
                (ignore_mask != 255).sum()
            total_mask_ratio.update(mask_ratio.item())

            iters = epoch * len(trainloader_u) + i
            lr = cfg['lr'] * (1 - iters / total_iters) ** 0.9
            optimizer.param_groups[0]["lr"] = lr
            optimizer.param_groups[1]["lr"] = lr * cfg['lr_multi']
            
            if accelerator.is_main_process:
                writer.add_scalar('train/loss_all', loss.item(), iters)
                writer.add_scalar('train/loss_x', loss_x.item(), iters)
                writer.add_scalar('train/loss_s', (loss_u_s1.item() + loss_u_s2.item()) / 2.0, iters)
                writer.add_scalar('train/loss_w_fp', loss_u_w_fp.item(), iters)
                writer.add_scalar('train/mask_ratio', mask_ratio, iters)
                end_batch_time = time.time()
                total_time_taken.update(end_batch_time - start_batch_time)
          
            log_interval = len(trainloader_u) // 8
            
            # if (i % 10 == 0) and accelerator.is_main_process:
            if (i % log_interval) == 0 and accelerator.is_main_process:
                eta_min = total_time_taken.avg * (len(trainloader_l) - 1 - i) / 60
                logger.info('Iters: [{:>4}/{}, Total loss: {:.3f}, Loss x: {:.3f}, Loss s: {:.3f}, Loss w_fp: {:.3f}, Mask ratio: '
                            '{:.3f}, eta: {:.0f}m, batch_avg: {:.2f}m'.format(i,len(trainloader_l), total_loss.avg, total_loss_x.avg, total_loss_s.avg,
                                            total_loss_w_fp.avg, total_mask_ratio.avg, eta_min, total_time_taken.avg / 60 ))

        eval_mode = 'sliding_window' if cfg['dataset'] == 'cityscapes' else 'original'
        if epoch % args.eval_interval == 0:
           
            mIoU, iou_class = evaluate(model, valloader, eval_mode, cfg, accelerator)

            if accelerator.is_main_process:
                for (cls_idx, iou) in enumerate(iou_class):
                    logger.info('***** Evaluation ***** >>>> Class [{:} {:}] '
                                'IoU: {:.2f}'.format(cls_idx, CLASSES[cfg['dataset']][cls_idx], iou))
                logger.info('***** Evaluation {} ***** >>>> MeanIoU: {:.2f}\n'.format(eval_mode, mIoU))
                
                writer.add_scalar('eval/mIoU', mIoU, epoch)
                for i, iou in enumerate(iou_class):
                    writer.add_scalar('eval/%s_IoU' % (CLASSES[cfg['dataset']][i]), iou, epoch)

            is_best = mIoU > previous_best
            previous_best = max(mIoU, previous_best)
            if accelerator.is_main_process:
                checkpoint = {
                    'model': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'epoch': epoch,
                    'previous_best': previous_best,
                }
                accelerator.save(checkpoint, os.path.join(args.save_path, 'latest.pth'))
                if is_best:
                    accelerator.save(checkpoint, os.path.join(args.save_path, 'best.pth'))
                    


if __name__ == '__main__':
    main()
