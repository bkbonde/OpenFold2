import argparse
import subprocess
from pathlib import Path
import pickle
import torch
from alphafold.Data.dataset import GeneralFileData, get_stream
from alphafold.Data.pipeline import DataPipeline
from alphafold.Model.features import AlphaFoldFeatures
from alphafold.Model.alphafold import AlphaFold
from alphafold.Model import model_config
from alphafold.Common import protein
from custom_config import tiny_config
import sys
import pytorch_lightning as pl
from pytorch_lightning.loggers import TensorBoardLogger
from typing import Any, Dict

class ExponentialMovingAverage:
	def __init__(self, model:torch.nn.Module, decay:float) -> None:
		self.params = {}
		for k, v in model.state_dict().items():
			self.params[k] = v.clone().detach()
			self.device = v.device
		self.decay = decay
	
	def to(self, device:torch.device):
		for k, v in self.params.items():
			self.params[k] = v.to(device=device)
		self.device = device
	
	def update(self, model:torch.nn.Module):
		update_params = model.state_dict()
		with torch.no_grad():
			for param_name, stored in self.params.items():
				diff = stored - update_params[param_name]
				stored -= diff*(1.0 - self.decay)
				

	def load_state_dict(self, state_dict):
		self.params = state_dict["params"]
		self.decay = state_dict["decay"]

	def state_dict(self):
		return {"params": self.params, "decay": self.decay}
		

class AlphaFoldModule(pl.LightningModule):
	def __init__(self, config):
		super().__init__()
		self.af2features = AlphaFoldFeatures(config=config, device='cuda:0', is_training=True)
		# self.af2features = AlphaFoldFeatures(config=config, device='cpu', is_training=True)
		self.af2 = AlphaFold(config=config.model, target_dim=22, msa_dim=49, extra_msa_dim=25, compute_loss=True)
		self.iter = 0
		self.ema = ExponentialMovingAverage(self.af2, 0.999)
		if self.ema.device != self.af2features.device:
			self.ema.to(self.af2features.device)

	def logging(self, ret):
		for head_name in ret.keys():
			if 'loss' in ret[head_name].keys():
				self.logger.experiment.add_scalar(f"Heads/{head_name}_loss", ret[head_name]['loss'].item(), self.iter)
		
		for key in ["fape", "sidechain_fape", "chi_loss", "angle_norm_loss"]:
			metric = ret['structure_module'][key]
			self.logger.experiment.add_scalar(f"Structure/Losses/{key}", metric.item(), self.iter)
		
		for metric_name in ret['structure_module']['metrics'].keys():
			metric = ret['structure_module']['metrics'][metric_name]
			self.logger.experiment.add_scalar(f"Structure/Metrics/{metric_name}", metric.item(), self.iter)

		
	def forward(self, feature_dict, pdb_path:Path=None):
		batch = self.af2features(feature_dict, random_seed=42)
		ret, total_loss = self.af2(batch, is_training=False, return_representations=False)
		
		if not(pdb_path is None):
			protein_pdb = protein.from_prediction(features=batch, result=ret)
			with open(pdb_path, 'w') as f:
				f.write(protein.to_pdb(protein_pdb))

		return ret, total_loss

	def training_step(self, feature_dict, batch_idx):
		batch = self.af2features(feature_dict, random_seed=42)
		ret, total_loss = self.af2(batch, is_training=False, return_representations=False)
		self.logging(ret)
		self.iter += 1
		return total_loss

	def configure_optimizers(self):
		optimizer = torch.optim.Adam(self.af2.parameters(), lr=1e-3, eps=1e-8)
		return optimizer

	def on_before_zero_grad(self, optimizer: torch.optim.Optimizer) -> None:
		self.ema.update(self.af2)
		return super().on_before_zero_grad(optimizer)
	
	def on_after_backward(self) -> None:
		# for k, param in zip(self.af2.state_dict().keys(), self.af2.parameters()):
		# 	if param.grad is None:
		# 		print(k, self.af2.state_dict()[k].size(), param.requires_grad)
		# 	# assert not(param.grad is None)
		# # for param in self.af2.parameters():
		# # 	print(param.size(), param.grad is None)
		# sys.exit()
		return super().on_after_backward()

	def on_save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
		checkpoint["ema"] = self.ema.state_dict()
		return super().on_save_checkpoint(checkpoint)

class DataModule(pl.LightningDataModule):
	def __init__(self, train_dataset_dir:Path) -> None:
		super(DataModule, self).__init__()
		self.data_train = GeneralFileData(train_dataset_dir, allowed_suffixes=['.pkl'])
		    
	def train_dataloader(self):
		def load_pkl(batch):
			file_path_list, = batch
			assert len(file_path_list) == 1
			with open(file_path_list[0], 'rb') as f:
				return pickle.load(f)

		return get_stream(self.data_train, batch_size=1, process_fn=load_pkl)
	
	def test_dataloader(self):
		def load_pkl(batch):
			file_path_list, = batch
			assert len(file_path_list) == 1
			with open(file_path_list[0], 'rb') as f:
				return pickle.load(f)

		return get_stream(self.data_train, batch_size=1, process_fn=load_pkl)

if __name__=='__main__':
	parser = argparse.ArgumentParser(description='Train deep protein docking')	
	# parser.add_argument('-dataset_dir', default='/media/HDD/AlphaFold2Dataset/Features', type=str)
	# parser.add_argument('-data_dir', default='/media/HDD/AlphaFold2', type=str)
	parser.add_argument('-dataset_dir', default='/media/lupoglaz/AlphaFold2Dataset/Features', type=str)
	parser.add_argument('-data_dir', default='/media/lupoglaz/AlphaFold2Data', type=str)
	parser.add_argument('-model_name', default='model_1', type=str)

	args = parser.parse_args()
	args.data_dir = Path(args.data_dir)
	args.dataset_dir = Path(args.dataset_dir)

	logger = TensorBoardLogger("LogTrain", name="tiny_config_wosv")
	data = DataModule(args.dataset_dir)
	model = AlphaFoldModule(tiny_config)
	trainer = pl.Trainer(gpus=1, logger=logger, max_epochs=10000)#, precision=16, amp_backend="native")
	trainer.fit(model, data)
	trainer.save_checkpoint(Path(trainer.logger.log_dir)/Path("checkpoints/final.ckpt"), weights_only=True)

	# ckpt = torch.load(Path("LogTrain/tiny_config_wosv/version_0/checkpoints/final.ckpt"))
	
	# model.load_state_dict(ckpt["state_dict"])
	# model.to(device='cuda:0')
	# model.eval()
	# data_stream = data.test_dataloader()
	# for feature_dict in data_stream:
	# 	with torch.no_grad():
	# 		prediction_result, loss = model(feature_dict, Path('test.pdb'))
	# 		print(loss)



