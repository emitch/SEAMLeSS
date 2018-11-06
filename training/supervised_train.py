#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""Train a network.

This is the main module which is invoked to train a network.

Running:
    To begin training, run

        $ python3 supervised_train.py start MODEL_NAME [...]

    or equivalently

        $ ./supervised_train.py start MODEL_NAME [...]

    To get help with the command line options, use

        $ python3 supervised_train.py start --help

Resuming:

    It is possible to resume training. This is useful if a training run was
    killed by accident or circumstance and you would like to continue training.
    Before doing this, check to make sure the run is actually in fact dead,
    since attempting to resume a live run could have undefined behavior.
    To resume training run

        $ python3 supervised_train.py resume MODEL_NAME

    where `MODEL_NAME` is the name of the previously stopped training run.

Example:
        $ python3 supervised_train.py start improved_net --lm 2 --hm 9

Editor's note:

    The top of this file should contain the comment `# PYTHON_ARGCOMPLETE_OK`
    in order to allow command line tab completion to work properly.

"""
from arguments import parse_args  # keep first for fast args access

import os
import sys
import time
import warnings
import datetime
import math
import random

import torch
import torch.backends.cudnn as cudnn
import torch.utils.data
import torchvision.transforms as transforms
from pathlib import Path

import masks as masklib
import stack_dataset
from archive import ModelArchive
from helpers import (gridsample_residual, save_chunk, dvl as save_vectors,
                     upsample, downsample, AverageMeter)
from loss import smoothness_penalty


def main():
    global state_vars
    args = parse_args()

    # set available GPUs
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_ids

    # create or load the model, optimizer, and parameters
    if args.command == 'start':
        if ModelArchive.model_exists(args.name):
            raise ValueError('The model "{}" already exists.'
                             .format(args.name))
        if args.saved_model is not None:
            # load a previous model and create a copy
            if not ModelArchive.model_exists(args.saved_model):
                raise ValueError('The model "{}" could not be found.'
                                 .format(args.saved_model))
            old = ModelArchive(args.saved_model, readonly=True)
            archive = old.start_new(readonly=False, **vars(args))
            # TODO: remove old model from memory
        else:
            archive = ModelArchive(readonly=False, **vars(args))
        state_vars = archive.state_vars
        state_vars['name'] = args.name
        state_vars['height'] = args.height
        state_vars['start_lr'] = args.lr
        state_vars['lr'] = args.lr
        state_vars['wd'] = args.wd
        state_vars['gamma'] = args.gamma
        state_vars['gamma_step'] = args.gamma_step
        state_vars['epoch'] = 0
        state_vars['iteration'] = None
        state_vars['num_epochs'] = args.num_epochs
        state_vars['epochs_per_mip'] = args.epochs_per_mip
        state_vars['training_set_path'] = Path(args.training_set).expanduser()
        state_vars['validation_set_path'] = (
            Path(args.validation_set).expanduser() if args.validation_set
            else None)
        state_vars['lm'] = args.lm
        state_vars['hm'] = args.hm
        state_vars['supervised'] = args.supervised
        state_vars['batch_size'] = args.batch_size
        state_vars['log_time'] = args.log_time
        state_vars['checkpoint_time'] = args.checkpoint_time
        state_vars['vis_time'] = args.vis_time
        state_vars['lambda1'] = args.lambda1
        state_vars['penalty'] = args.penalty
        state_vars['gpus'] = args.gpu_ids
        log_titles = [
            'Time Stamp',
            'Epoch',
            'Iteration',
            'Training Loss',
            'Validation Loss',
        ]
        archive.set_log_titles(log_titles)
        archive.set_optimizer_params(learning_rate=args.lr,
                                     weight_decay=args.wd)

        # save initialized state to archive; create first checkpoint
        archive.save()
        archive.create_checkpoint(epoch=None, iteration=None)
    else:  # args.command == 'resume'
        if not ModelArchive.model_exists(args.name):
            raise ValueError('The model "{}" could not be found.'
                             .format(args.name))
        archive = ModelArchive(args.name, readonly=False)
        state_vars = archive.state_vars
        state_vars['gpus'] = args.gpu_ids

    # redirect output to the archive
    sys.stdout = archive.out
    sys.stderr = archive.err

    # optimize cuda processes
    cudnn.benchmark = True

    # Data loading code
    train_transform = transforms.Compose([
        stack_dataset.ToFloatTensor(),
        stack_dataset.Normalize(),
        stack_dataset.Contrast(),
        stack_dataset.RandomRotateAndScale(),
        stack_dataset.RandomFlip(),
        stack_dataset.Split(),
    ])
    train_dataset = stack_dataset.compile_dataset(
        state_vars.training_set_path, transform=train_transform)
    train_sampler = torch.utils.data.RandomSampler(train_dataset)
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=state_vars.batch_size,
        shuffle=(train_sampler is None), num_workers=args.num_workers,
        pin_memory=True, sampler=train_sampler)

    if state_vars.validation_set_path:
        val_transform = transforms.Compose([
            stack_dataset.ToFloatTensor(),
            stack_dataset.Normalize(),
            stack_dataset.Contrast(),
            stack_dataset.RandomRotateAndScale(),
            stack_dataset.RandomFlip(),
            stack_dataset.Split(),
        ])
        validation_dataset = stack_dataset.compile_dataset(
            state_vars.validation_set_path, transform=val_transform)
        val_loader = torch.utils.data.DataLoader(
            validation_dataset, batch_size=1,
            shuffle=False, num_workers=args.num_workers, pin_memory=True)
    else:
        val_loader = None

    # Averaging
    train_losses = AverageMeter()
    val_losses = AverageMeter()
    epoch_time = AverageMeter()

    print('=========== BEGIN TRAIN LOOP ============')
    start_epoch = state_vars.epoch
    for epoch in range(start_epoch, state_vars.num_epochs):
        start_time = time.time()
        state_vars.epoch = epoch
        archive.save()

        # train for one epoch
        train_loss = train(train_loader, archive, epoch)
        train_losses.update(train_loss)

        # evaluate on validation set
        if val_loader:
            val_loss = validate(val_loader, archive, epoch)
            val_losses.update(val_loss)
        else:
            val_loss = None

        # log and save state
        state_vars.epoch = epoch + 1
        state_vars.iteration = None
        archive.save()
        log_values = [
            datetime.datetime.now(),
            epoch,
            len(train_loader),
            '',  # train_loss
            val_loss if val_loss is not None else '',
        ]
        archive.log(log_values, printout=False)
        archive.create_checkpoint(epoch, iteration=None)
        epoch_time.update(time.time() - start_time)
        print('{0}\t'
              'Completed Epoch {1}\t'
              'TrainLoss {train_losses.val:.10f} ({train_losses.avg:.10f})\t'
              'ValLoss {val_losses.val:.10f} ({val_losses.avg:.10f})\t'
              'EpochTime {epoch_time.val:.3f} ({epoch_time.avg:.3f})\t'
              '\n'
              .format(state_vars.name, epoch, train_losses=train_losses,
                      val_losses=val_losses, epoch_time=epoch_time))


def train(train_loader, archive, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()

    # switch to train mode and select the submodule to train
    archive.model.train()
    archive.adjust_learning_rate()
    submodule = select_submodule(archive.model, epoch, init=True)
    max_disp = submodule.module.pixel_size_ratio * 2  # correct 2-pixel disp

    start_time = time.time()
    if state_vars.iteration is None:
        start_iter = 0
    else:
        start_iter = state_vars.iteration

    for i, sample in enumerate(train_loader, start_iter):
        if i >= len(train_loader):
            break
        state_vars.iteration = i

        # measure data loading time
        data_time.update(time.time() - start_time)

        # compute output and loss
        src, tgt, truth = prepare_input(sample, max_displacement=max_disp)
        prediction = submodule(src, tgt)
        masks = gen_masks(src, tgt, prediction)
        if truth is not None:
            loss = supervised_loss(prediction=prediction, truth=truth, **masks)
        else:
            loss = unsupervised_loss(src, tgt, prediction=prediction, **masks)

        # compute gradient and do optimizer step
        archive.optimizer.zero_grad()
        loss.backward()
        archive.optimizer.step()
        loss = loss.item()  # get python value without the computation graph
        losses.update(loss)
        state_vars.iteration = i + 1  # advance iteration to resume right
        archive.save()

        # measure elapsed time
        batch_time.update(time.time() - start_time)

        # logging and checkpointing
        if state_vars.vis_time and i % state_vars.vis_time == 0:
            try:
                debug_dir = archive.new_debug_directory(epoch, i)
                save_chunk(src[0:1, ...], str(debug_dir / 'src'))
                save_chunk(src[0:1, ...], str(debug_dir / 'xsrc'))  # xtra copy
                save_chunk(tgt[0:1, ...], str(debug_dir / 'tgt'))
                warped_src = gridsample_residual(
                    src[0:1, ...],
                    prediction[0:1, ...].detach().to(src.device),
                    padding_mode='zeros')
                save_chunk(warped_src[0:1, ...], str(debug_dir / 'warped_src'))
                archive.visualize_loss('Training Loss', 'Validation Loss')
                save_vectors(prediction[0:1, ...].detach(),
                             str(debug_dir / 'prediction'))
                if truth is not None:
                    save_vectors(truth[0:1, ...].detach(),
                                 str(debug_dir / 'ground_truth'))
                for k, v in masks.items():
                    if v is not None and len(v) > 0:
                        save_chunk(v[0][0:1, ...], str(debug_dir / k))
            except Exception as e:
                # Don't raise the exception, since visualization issues
                # should not stop training. Just warn the user and go on.
                print('Visualization failed: {}: {}'
                      .format(e.__class__.__name__, e))
        if (state_vars.checkpoint_time
                and i % state_vars.checkpoint_time == 0):
            archive.create_checkpoint(epoch=epoch, iteration=i)
        if state_vars.log_time and i % state_vars.log_time == 0:
            log_values = [
               datetime.datetime.now(),
               epoch,
               i,
               loss,
               '',
            ]
            archive.log(log_values, printout=False)
            print('{0}\t'
                  'Epoch: {1} [{2}/{3}]\t'
                  'Loss {loss.val:.10f} ({loss.avg:.10f})\t'
                  'BatchTime {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'DataTime {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  .format(
                      state_vars.name,
                      epoch, i, len(train_loader), batch_time=batch_time,
                      data_time=data_time, loss=losses))

        start_time = time.time()
    return losses.avg


@torch.no_grad()
def validate(val_loader, archive, epoch):
    losses = AverageMeter()

    # switch to evaluate mode
    archive.model.eval()
    submodule = select_submodule(archive.model, epoch)

    start_time = time.time()
    # compute output and loss
    for i, sample in enumerate(val_loader):
        print('{0}\t'
              'Validation: [{1}/{2}]\t'
              .format(state_vars.name, i, len(val_loader)), end='\r')
        src, tgt, truth = prepare_input(sample, supervised=False)
        prediction = submodule(src, tgt)
        masks = gen_masks(src, tgt, prediction)
        loss = unsupervised_loss(src, tgt, prediction=prediction, **masks)
        losses.update(loss.item())

    # measure elapsed time
    batch_time = (time.time() - start_time)

    print('{0}\t'
          'Validation: [{1}/{1}]\t'
          'Loss {loss.avg:.10f}\t\t\t'
          'Time {batch_time:.3f}\t'
          .format(state_vars.name, len(val_loader),
                  batch_time=batch_time, loss=losses))

    return losses.avg


def select_submodule(model, epoch, init=False):
    """
    Selects the submodule to be trained based on the current epoch.
    At epoch `epoch`, train level `epoch/epochs_per_mip` of the model.
    """
    if epoch is None:
        return model
    index = epoch // state_vars.epochs_per_mip
    submodule = model.module[:index+1].train_last()
    if (init and epoch % state_vars.epochs_per_mip == 0
            and index < state_vars.height
            and state_vars.iteration is None):
        submodule.init_last()
    return torch.nn.DataParallel(submodule)


@torch.no_grad()
def prepare_input(sample, supervised=None, max_displacement=2):
    """
    Formats the input received from the data loader and produces a
    ground truth vector field if supervised.
    If `supervised` is None, it uses the value specified in state_vars
    """
    if supervised is None:
        supervised = state_vars.supervised
    if supervised:
        src = sample['src'].cuda()
        truth_field = random_field(src.shape, max_displacement=max_displacement)
        tgt = gridsample_residual(src, truth_field, padding_mode='zeros')
    else:
        src = sample['src'].cuda()
        tgt = sample['tgt'].cuda()
        truth_field = None
    return src, tgt, truth_field


@torch.no_grad()
def random_field(shape, max_displacement=2, num_downsamples=7):
    """
    Genenerates a random vector field smoothed by bilinear interpolation.

    The vectors generated will have values representing displacements of
    between (approximately) `-max_displacement` and `max_displacement` pixels
    at the size dictated by `shape`.
    The actual values, however, will be scaled to the spatial transformer
    standard, where -1 and 1 represent the edges of the image.

    `num_downsamples` dictates the block size for the random field.
    Each block will have size `2**num_downsamples`.
    """
    zero = torch.zeros(shape, device='cuda')
    zero = torch.cat([zero, zero.clone()], 1)
    smaller = downsample(num_downsamples)(zero)
    std = max_displacement / shape[-2] / math.sqrt(2)
    field = torch.nn.init.normal_(smaller, mean=0, std=std)
    field = upsample(num_downsamples)(field)
    result = field.permute(0, 2, 3, 1)
    return result


def supervised_loss(prediction, truth, **masks):
    """
    Calculate a supervised loss based on the mean squared error with
    the ground truth vector field.
    """
    truth = truth.to(prediction.device)
    return ((prediction - truth) ** 2).mean()


def unsupervised_loss(src, tgt, prediction,
                      src_masks=None, tgt_masks=None,
                      src_field_masks=None, tgt_field_masks=None):
    """
    Calculate a self-supervised loss based on
    (a) the mean squared error between the source and target images
    (b) the smoothness of the vector field

    The masks are used to ignore or reduce the loss values in certain regions
    of the images and vector field.

    If `MSE(a, b)` is the mean squared error of two images, and `Penalty(f)`
    is the smoothness penalty of a vector field, the loss is calculated
    roughly as
        >>> loss = MSE(src, tgt) + lambda1 * Penalty(prediction)
    where `lambda1` and the type of smoothness penalty are both
    pulled from the `state_vars` dictionary.
    """
    src, tgt = src.to(prediction.device), tgt.to(prediction.device)

    src_warped = gridsample_residual(src, prediction, padding_mode='zeros')
    image_loss_map = (src_warped - tgt)**2
    if src_masks or tgt_masks:
        image_weights = torch.ones_like(image_loss_map)
        if src_masks is not None:
            for mask in src_masks:
                mask = gridsample_residual(mask, prediction,
                                           padding_mode='border')
                image_loss_map = image_loss_map * mask
                image_weights = image_weights * mask
        if tgt_masks is not None:
            for mask in tgt_masks:
                image_loss_map = image_loss_map * mask
                image_weights = image_weights * mask
        mse_loss = image_loss_map.sum() / image_weights.sum()
    else:
        mse_loss = image_loss_map.mean()

    field_penalty = smoothness_penalty(state_vars.penalty)
    field_loss_map = field_penalty([prediction])
    if src_field_masks or tgt_field_masks:
        field_weights = torch.ones_like(field_loss_map)
        if src_field_masks is not None:
            for mask in src_field_masks:
                mask = gridsample_residual(mask, prediction,
                                           padding_mode='border')
                field_loss_map = field_loss_map * mask
                field_weights = field_weights * mask
        if tgt_field_masks is not None:
            for mask in tgt_field_masks:
                field_loss_map = field_loss_map * mask
                field_weights = field_weights * mask
        field_loss = field_loss_map.sum() / field_weights.sum()
    else:
        field_loss = field_loss_map.mean()

    loss = (mse_loss + state_vars.lambda1 * field_loss) / 25000
    return loss


@torch.no_grad()
def gen_masks(src, tgt, prediction, threshold=10):
    """
    Returns masks with which to weight the loss function
    """
    src, tgt = src.to(prediction.device), tgt.to(prediction.device)
    src, tgt = (src * 255).to(torch.uint8), (tgt * 255).to(torch.uint8)

    src_mask, tgt_mask = torch.ones_like(src), torch.ones_like(tgt)

    src_mask_zero, tgt_mask_zero = (src < threshold), (tgt < threshold)
    src_mask_five = masklib.dilate(src_mask_zero, radius=3)
    tgt_mask_five = masklib.dilate(tgt_mask_zero, radius=3)
    src_mask[src_mask_five], tgt_mask[tgt_mask_five] = 5, 5
    src_mask[src_mask_zero], tgt_mask[tgt_mask_zero] = 0, 0

    src_field_mask, tgt_field_mask = torch.ones_like(src), torch.ones_like(tgt)
    src_field_mask[src_mask_zero], tgt_field_mask[tgt_mask_zero] = 0, 0

    return {'src_masks': [src_mask.float()],
            'tgt_masks': [tgt_mask.float()],
            'src_field_masks': [src_field_mask.float()],
            'tgt_field_masks': [tgt_field_mask.float()]}


if __name__ == '__main__':
    main()
