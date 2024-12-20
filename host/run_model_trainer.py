import cv2
import numpy as np
import os
import os.path as osp
import torch
import argparse
import math
import copy
import time
import torch.nn.functional as F
import torchvision.transforms as T

from mmseg.apis import init_model
from mmseg.utils import register_all_modules, SampleList, dataset_aliases, get_classes, get_palette
from mmengine.config import Config, DictAction
from mmengine.runner import Runner, load_checkpoint
from mmengine.registry import MODELS, EVALUATOR, METRICS
from mmseg.models.utils import Upsample, resize
from mmseg.evaluation import IoUMetric
from mmseg.models.backbones import set_requires_grad

import rein
import copy
from PIL import Image

def cross_entropy(x, y):# -> torch.Tensor:
    return -(y.softmax(1) * x.log_softmax(1)).sum(1)


class ModelTrainer():
    def __init__(self, cloud_model, device_model, source_buffer, image_folder_path, checkpoint_folder_path, resize_shape_cloud=(512, 1024), use_flip = False, use_ema = False, use_conf = False):
        self.cloud_model = cloud_model
        self.device_model = device_model
        self.source_buffer = source_buffer

        self.image_folder = image_folder_path
        self.checkpoint_folder = checkpoint_folder_path
        self.current_ckpt = 'ckpt.pth'

        self.resize_shape_device = (512, 1024)
        self.resize_shape_cloud = resize_shape_cloud
        self.use_ema = use_ema
        self.use_flip = use_flip
        # build a inter-process communication with zmq
        # context =zmq.Context()
        # self.socket = context.socket(zmq.REQ)
        # self.socket.connect("tcp://localhost:5555")   
        self.batch_size = 8
        
        # mean = [0.485, 0.456, 0.406]
        # std = [0.229, 0.224, 0.225]
        self.transform = T.Compose([
            T.ToTensor(),
            # T.Normalize(mean, std)
            ])

        params = []
        names = []
        for name, param in self.device_model.named_parameters():
            if param.requires_grad: #
                params.append(param)
                names.append(name)
        print(names)
        self.optimizer = torch.optim.Adam(params, lr=0.00006, betas=(0.9, 0.999))
        # self.optimizer = torch.optim.SGD(params, lr=0.00001)
        self.use_conf = use_conf
        if use_ema:
            self.ema_model = copy.deepcopy(device_model)
        self.source_model = copy.deepcopy(device_model)
            

    def run(self):
        while True:
            if self._check_image_folder():
                self._run_distillation()
                self._save_checkpoint()
            else:
                time.sleep(1)

    def _check_image_folder(self):
        imgs = os.listdir(self.image_folder)
        # print(len(imgs))
        return not (len(imgs) == 0)
    
    def _load_images(self):
        imgs = os.listdir(self.image_folder)
        imgs.sort()
        # print(imgs)
        i = 0
        images = []
        flip_images = []
        for img in imgs[::-1]:
            # print(img)
            img_ = Image.open(os.path.join(self.image_folder, img))
            img = self.transform(img_).unsqueeze(0) * 255
            images.append(img)        

            if self.use_flip:
                img = self.transform(img_.transpose(Image.Transpose.FLIP_LEFT_RIGHT)).unsqueeze(0) * 255
                flip_images.append(img)   
            i += 1
            if i == self.batch_size:
                break
        images = torch.cat(images, dim=0)
        if self.use_flip:
            flip_images = torch.cat(flip_images, dim=0)
        print(images.shape)
        
        return images, flip_images


def parse_args():
    parser = argparse.ArgumentParser(
        description='MMSeg test (and eval) a model')
    parser.add_argument('config', help='train config file path')
    parser.add_argument('checkpoint', help='checkpoint file')
    parser.add_argument('--config_t', help='train config file path')
    parser.add_argument('--checkpoint_t', help='checkpoint file')
    parser.add_argument('--backbone', help='checkpoint file')
    parser.add_argument('--cloud_model_shape', type=int, help='size of input for cloud model')
    parser.add_argument('--enable_ema', action='store_true', default=False, help='using ema for device model')
    parser.add_argument('--enable_conf', action='store_true', default=False, help='using confidence for device model')
    parser.add_argument('--enable_flip', action='store_true', default=False, help='using flip for cloud model')

    args = parser.parse_args()
    return args

def main():
    args = parse_args()
    print('CONFIG: ', args.config, args.config_t)
    print('CHECKPOINT: ', args.checkpoint, args.checkpoint_t)

    # load config
    cfg = Config.fromfile(args.config)
    cfg.load_from = args.checkpoint

    cfg_cm = Config.fromfile(args.config_t)
    cfg_cm.load_from = args.checkpoint_t

    register_all_modules()
    device = 'cuda:0'

    cloud_model = MODELS.build(cfg_cm.model)
    if args.backbone:
        checkpoint = load_checkpoint(cloud_model.backbone, args.backbone, map_location='cpu')
    checkpoint = load_checkpoint(cloud_model, cfg_cm.load_from, map_location='cpu')
    cloud_model.dataset_meta = {
                'classes': get_classes('cityscapes'),
                'palette': get_palette('cityscapes')
            }
    cloud_model.to(device)
    cloud_model.eval()

    device_model = MODELS.build(cfg.model)
    checkpoint = load_checkpoint(device_model, cfg.load_from, map_location='cpu')
    device_model.dataset_meta = {
                'classes': get_classes('cityscapes'),
                'palette': get_palette('cityscapes')
            }
    device_model.to(device)
    device_model.eval()

    # set_requires_grad(device_model, ['adaptformer'])
    testloader_clear = Runner.build_dataloader(cfg['train_dataloader'])

    cloud_model_input_shape = (512, 1024) if args.cloud_model_shape == 512 else (1024, 1024)

    print('cloud_model parameters', sum(p.numel() for p in cloud_model.parameters()))
    print('device_model parameters', sum(p.numel() for p in device_model.parameters()))


    trainer = ModelTrainer(
        cloud_model=cloud_model,
        device_model=device_model,
        source_buffer=testloader_clear,
        image_folder_path='./host/images',
        checkpoint_folder_path='./host/checkpoints',
        resize_shape_cloud = cloud_model_input_shape,
        use_flip= args.enable_flip,
        use_ema = args.enable_ema,
        use_conf = args.enable_conf
    )
    trainer.run()


if __name__ == '__main__':
    main()