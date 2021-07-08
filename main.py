from argparse import ArgumentParser

import kornia as K
import pytorch_lightning as pl
from adabelief_pytorch import AdaBelief
from pytorch_lightning.callbacks import BackboneFinetuning, ModelCheckpoint, EarlyStopping, StochasticWeightAveraging
from pytorch_lightning.core.lightning import LightningModule
from pytorch_lightning.plugins import DDPPlugin
from torch import Tensor

from dataloading import AlgonautsMINIDataModule
from model_i3d import *
from sam import SAM
from utils import *
from pyramidpooling import *


# from torchmetrics.utilities import rank_zero_warn
#
#
# def _pearson_corrcoef_update(
#         preds: Tensor,
#         target: Tensor,
#         *_,
# ) -> Tuple[Tensor, Tensor]:
#     """ updates current estimates of the mean, cov and n_obs with new data for calculating pearsons correlation """
#     # Data checking
#     _check_same_shape(preds, target)
#     preds = preds.squeeze()
#     target = target.squeeze()
#     if preds.ndim > 2 or target.ndim > 2:
#         raise ValueError('Expected both predictions and target to be 2 dimensional tensors.')
#
#     return preds, target
#
#
def _pearson_corrcoef_compute(preds: Tensor, target: Tensor, eps: float = 1e-6) -> Tensor:
    """ computes the final pearson correlation based on covariance matrix and number of observatiosn """
    dim = 1
    preds_diff = preds - preds.mean(dim)
    target_diff = target - target.mean(dim)

    cov = (preds_diff * target_diff).mean(dim)
    preds_std = torch.sqrt((preds_diff * preds_diff).mean(dim))
    target_std = torch.sqrt((target_diff * target_diff).mean(dim))

    denom = preds_std * target_std
    # prevent division by zero
    if denom == 0:
        denom += eps

    corrcoef = cov / denom
    return torch.clamp(corrcoef, -1.0, 1.0)


def vectorized_correlation(x, y):
    dim = 0

    centered_x = x - x.mean(dim, keepdims=True)
    centered_y = y - y.mean(dim, keepdims=True)

    covariance = (centered_x * centered_y).sum(dim, keepdims=True)

    bessel_corrected_covariance = covariance / (x.shape[dim] - 1)

    x_std = x.std(dim, keepdims=True) + 1e-8
    y_std = y.std(dim, keepdims=True) + 1e-8

    corr = bessel_corrected_covariance / (x_std * y_std)

    return corr.ravel()


class DataAugmentation(nn.Module):
    """Module to perform data augmentation using Kornia on torch tensors."""

    def __init__(self) -> None:
        super().__init__()

        self.transforms = nn.Sequential(
            K.augmentation.RandomHorizontalFlip3D(p=0.5),
            K.augmentation.RandomRotation3D(degrees=15, p=0.5),
            # K.augmentation.RandomAffine3D(p=0.5,
            #                               degrees=15,
            #                               translate=(0.1, 0.1, 0),
            #                               )
        )

    @torch.no_grad()  # disable gradients for efficiency
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x_out = self.transforms(x)  # BxCxDxHxW
        return x_out


class LitI3DFC(LightningModule):

    def __init__(self, backbone, hparams: dict, *args, **kwargs):
        super(LitI3DFC, self).__init__()
        self.save_hyperparameters(hparams)
        # self.hparams = hparams
        self.lr = self.hparams.learning_rate

        self.automatic_optimization = False

        # self.train_transform = DataAugmentation()
        self.train_transform = None

        self.backbone = backbone

        # self.backbone = nn.SyncBatchNorm.convert_sync_batchnorm(backbone) # slooooow

        self.minifc = MiniFC(hparams)

    @staticmethod
    def add_model_specific_args(parser):
        parser.add_argument_group("LitModel")
        parser.add_argument('--conv_size', type=int, default=256)
        parser.add_argument('--num_layers', type=int, default=2)
        parser.add_argument('--activation', type=str, default='elu')
        parser.add_argument('--layer_hidden', type=int, default=2048)
        parser.add_argument('--dropout_rate', type=float, default=0.0)
        parser.add_argument('--weight_decay', type=float, default=1e-2)
        parser.add_argument('--learning_rate', type=float, default=3e-4)
        parser.add_argument('--backbone_lr_ratio', type=float, default=0.1)
        parser.add_argument('--pooling_mode', type=str, default='avg')
        parser.add_argument('--softpool', default=False, action="store_true")
        parser.add_argument('--fc_batch_norm', default=False, action="store_true")
        return parser

    def forward(self, x):
        x3 = self.backbone(x)
        out = self.minifc(x3)
        return out

    def _shared_train_val(self, batch, batch_idx, prefix, is_log=True):
        x, y = batch
        out = self(x)
        loss = F.mse_loss(out, y)
        if is_log:
            self.log(f'{prefix}_mse_loss', loss,
                     on_step=True, on_epoch=True, prog_bar=True, logger=True, sync_dist=True)
        return out, loss

    # def training_step(self, batch, batch_idx):
    #     x, y = batch
    #     # x = self.train_transform(x)
    #     batch = (x, y)
    #
    #     out, loss = self._shared_train_val(batch, batch_idx, 'train')
    #     return loss

    def training_step(self, batch, batch_idx):
        self.train()
        x, y = batch
        x = self.train_transform(x) if self.train_transform is not None else x
        batch = (x, y)

        optimizer = self.optimizers()

        out, loss = self._shared_train_val(batch, batch_idx, 'train')
        self.manual_backward(loss)  # take care fp16

        if self.hparams.asm:
            def disable_bn(model):
                for module in model.modules():
                    if isinstance(module, nn.BatchNorm3d):
                        module.eval()

            optimizer.first_step(zero_grad=True)
            self.backbone.apply(disable_bn)
            out, loss = self._shared_train_val(batch, batch_idx, 'train', is_log=False)
            self.manual_backward(loss)  # take care fp16
            optimizer.second_step(zero_grad=True)
        else:
            optimizer.step()

    def validation_step(self, batch, batch_idx):
        out, loss = self._shared_train_val(batch, batch_idx, 'val')
        y = batch[-1]
        # self.val_corr(out[:, 0], y[:, 0])
        return {'out': out, 'y': y}

    def validation_epoch_end(self, val_step_outputs) -> None:
        # print("hello there")
        # val_corr = self.val_corr.compute()
        # self.log('val_corr', val_corr, prog_bar=True, logger=True)
        # val_outs = {k: torch.cat(v, 0) for k, v in val_step_outputs.items()}
        val_outs = torch.cat([out['out'] for out in val_step_outputs], 0)
        val_ys = torch.cat([out['y'] for out in val_step_outputs], 0)
        val_corr = vectorized_correlation(val_outs, val_ys).mean()
        self.log('val_corr', val_corr, prog_bar=True, logger=True, sync_dist=True)

    def configure_optimizers(self):
        """Prepare optimizer and schedule (linear warmup and decay)"""
        no_decay = ["bias", "BatchNorm3D.weight", "BatchNorm1D.weight", "BatchNorm2D.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in self.backbone.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": self.hparams.weight_decay,
                'lr': self.hparams.learning_rate * self.hparams.backbone_lr_ratio,
            },
            {
                "params": [p for n, p in self.backbone.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
                'lr': self.hparams.learning_rate * self.hparams.backbone_lr_ratio,
            },
            {
                "params": [p for n, p in self.minifc.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": self.hparams.weight_decay,
                'lr': self.hparams.learning_rate,
            },
            {
                "params": [p for n, p in self.minifc.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
                'lr': self.hparams.learning_rate,
            },
        ]
        # optimizer_grouped_parameters = filter(lambda p: p.requires_grad, self.parameters())
        # optimizer_grouped_parameters = [
        #     {
        #         "params": [p for p in self.backbone.parameters()],
        #         "lr": 1e-4,
        #     },
        #     {
        #         "params": [p for p in self.conv31.parameters()],
        #         "lr": 3e-4,
        #     },
        #     {
        #         "params": [p for p in self.fc.parameters()],
        #         "lr": 3e-4,
        #     },
        # ]
        if not self.hparams.asm:
            optimizer = AdaBelief(optimizer_grouped_parameters)
            # optimizer = SGD(optimizer_grouped_parameters, lr=self.lr, momentum=0.9, weight_decay=self.hparams.weight_decay)
            # sch = CosineAnnealingLR(optimizer, self.hparams.max_epochs)
        else:
            optimizer = SAM(optimizer_grouped_parameters, AdaBelief, adaptive=True, rho=0.5)

        return [optimizer], []


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('--video_frames', type=int, default=16)
    parser.add_argument('--video_size', type=int, default=288)
    parser.add_argument('--batch_size', type=int, default=8)
    parser.add_argument('--max_epochs', type=int, default=300)
    parser.add_argument('--datasets_dir', type=str, default='/data_smr/huze/projects/my_algonauts/datasets/')
    parser.add_argument('--roi', type=str, default="EBA")
    parser.add_argument('--backbone_freeze_epochs', type=int, default=100)
    parser.add_argument('--gpus', type=str, default='1')
    parser.add_argument('--val_check_interval', type=float, default=1.0)
    parser.add_argument('--val_ratio', type=float, default=0.1)
    parser.add_argument('--val_random_split', default=False, action="store_true")
    parser.add_argument('--save_checkpoints', default=False, action="store_true")
    parser.add_argument('--early_stop_epochs', type=int, default=10)
    parser.add_argument('--cached', default=False, action="store_true")
    parser.add_argument("--fp16", default=False, action="store_true")
    parser.add_argument("--asm", default=False, action="store_true")
    parser.add_argument('--predictions_dir', type=str, default='./predictions/v1/')


    parser = LitI3DFC.add_model_specific_args(parser)
    args = parser.parse_args()
    hparams = vars(args)

    dm = AlgonautsMINIDataModule(batch_size=args.batch_size, datasets_dir=args.datasets_dir, roi=args.roi,
                                 num_frames=args.video_frames, resolution=args.video_size,
                                 cached=args.cached, val_ratio=args.val_ratio, random_split=args.val_random_split)
    dm.setup()
    hparams['output_size'] = dm.num_voxels

    checkpoint_callback = ModelCheckpoint(
        monitor='val_corr',
        dirpath='/home/huze/.cache/checkpoints',
        filename='MiniFC-{epoch:02d}-{val_corr:.6f}',
        save_weights_only=True,
        save_top_k=2,
        mode='max',
    )

    early_stop_callback = EarlyStopping(
        monitor='val_corr',
        min_delta=0.00,
        patience=int(args.early_stop_epochs/args.val_check_interval),
        verbose=False,
        mode='max'
    )

    finetune_callback = BackboneFinetuning(args.backbone_freeze_epochs)

    callbacks = [early_stop_callback, finetune_callback]
    if args.save_checkpoints:
        callbacks.append(checkpoint_callback)

    trainer = pl.Trainer(
        precision=16 if args.fp16 else 32,
        gpus=args.gpus,
        # accelerator='ddp',
        # plugins=DDPPlugin(find_unused_parameters=False),
        # limit_train_batches=0.2,
        # limit_val_batches=0.2,
        # limit_test_batches=0.3,
        max_epochs=args.max_epochs,
        checkpoint_callback=args.save_checkpoints,
        val_check_interval=args.val_check_interval,
        callbacks=callbacks,
        # auto_lr_find=True,
    )

    backbone = modify_resnets_patrial_x3(multi_resnet3d50())

    plmodel = LitI3DFC(backbone, hparams)

    # trainer.tune(plmodel, datamodule=dm)
    trainer.fit(plmodel, dm)

    if args.save_checkpoints:
        plmodel = LitI3DFC.load_from_checkpoint(checkpoint_callback.best_model_path, backbone=backbone, hparams=hparams)
        prediction = trainer.predict(plmodel, datamodule=dm)

        torch.save(prediction, os.path.join(args.predictions_dir, f'{args.roi}.pt'))
        torch.save(checkpoint_callback.best_model_score,
                   os.path.join(args.predictions_dir, f'{args.roi}-score-{checkpoint_callback.best_model_score:.6f}'))