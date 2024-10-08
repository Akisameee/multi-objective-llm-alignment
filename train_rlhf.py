import os
# os.environ['CUDA_VISIBLE_DEVICES'] = '7'
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from einops import rearrange, repeat, reduce
from collections import deque
from functools import partial
from random import randrange
from tqdm import tqdm
from trl import AutoModelForCausalLMWithValueHead
from accelerate import Accelerator, dispatch_model

from base_trainer import Base_Trainer
from datas.instruct_dataset import Instruct_Pref_Dataset, instruct_prompt_collator
from configs import RLHF_Config, model_infos
# from modules.lms import BaseLM, RewardLM, get_model
from modules import BaseLMWithValueHeads
from modules.ppo import PPO_Trainer, PPOMemory
from modules.pefts import replace_peft_layers, set_all_adapters
from modules.utils import shift, log_prob, default, masked_mean, merge_dict
from logger import Logger
from trl.core import LengthSampler

TEST = 0

class RLHF_Trainer(Base_Trainer):

    def __init__(
        self,
        config: RLHF_Config
    ):
        config.model_cfg.model_class = BaseLMWithValueHeads
        super().__init__(
            config = config,
            accelerator_cfg = config.accelertor_cfg,
            model_cfg = config.model_cfg,
            ref_cfg = config.ref_cfg,
            reward_cfg = config.reward_cfg
        )

        self.ppo_trainer = PPO_Trainer(
            config = config,
            model = self.model,
            optimizer = self.optimizer,
            accelerator = self.accelerator,
            logger = self.logger
        )
        
        self.n_sample_reuse = config.n_sample_reuse
        self.kl_ref_coef = config.kl_ref_coef
        self.n_save_time = config.n_save_time
    
    def compute_reward_single(
        self,
        rm_reward: torch.FloatTensor,
        action_logprobs: torch.FloatTensor,
        ref_logprobs: torch.FloatTensor
    ):
        kl_ref = self.kl_penalty(action_logprobs, ref_logprobs) * self.kl_ref_coef
        reward = -kl_ref
        reward[:, -1] += rm_reward
        kl_ref_mean = -torch.mean(kl_ref).unsqueeze(0)
        
        return reward, kl_ref_mean, kl_ref_mean + rm_reward
        
    def kl_penalty(
        self,
        logprob,
        ref_logprob
    ):
        return logprob - ref_logprob

    def compute_action_log_prob(
        self,
        action_len,
        action_logits,
        sequence
    ):
        # need to shift along sequence dimension by 1, since actions start from the last prompt (state) token
        action_logits = shift(action_logits, shift = 1, dim = -2)
        action_prob = action_logits.softmax(dim = -1)

        action_log_prob = log_prob(action_prob, sequence)
        action_log_prob = action_log_prob[:, -action_len:]

        return action_log_prob, action_prob

    @torch.no_grad()
    def get_rm_rewards(
        self,
        reward_model: nn.Module,
        sequences: torch.FloatTensor,
        masks: torch.LongTensor,
        action_masks: torch.LongTensor,
        verbose: bool = False
    ):
        
        prompt_masks = (1 - action_masks) * masks
        prompt_texts, response_texts = self.ppo_trainer.decode_batch(
            inputs_ids = sequences,
            attention_masks = masks,
            prompt_masks = prompt_masks
        )
        rm_rewards = reward_model.get_rewards(
            prompts = prompt_texts,
            responses = response_texts
        )
        if verbose:
            self.logger.info(
                '-' * 50 + '\n' + \
                f'Prompt:{prompt_texts[0]}\n' + \
                '-' * 50 + '\n' + \
                f'Response:{response_texts[0]}\n' + \
                '-' * 50 + '\n' + \
                f'RM_Score:{rm_rewards[0]}'
            )

        return rm_rewards
    
    @torch.no_grad()
    def get_ref_logprobs(
        self,
        ref_model: nn.Module,
        sequences: torch.LongTensor,
        masks: torch.LongTensor
    ):
        
        if ref_model is None:
            # disable peft layers, switch to reference model
            set_all_adapters(
                model = self.ppo_trainer.model,
                enable = False
            )
            ref_logits, _ = self.ppo_trainer.batch_forward(
                sequences,
                mask = masks
            )
            set_all_adapters(
                model = self.ppo_trainer.model,
                enable = True
            )
        else:
            ref_logits, _, _ = ref_model(
                input_ids = sequences,
                attention_mask = masks
            )
        ref_logits = shift(ref_logits, shift = 1, dim = -2)
        ref_probs = ref_logits.softmax(dim = -1)
        ref_logprobs = log_prob(ref_probs, sequences)

        return ref_logprobs

    def train(
        self,
        ds_generator,
        n_episode: int = 10,
        n_update_timestep: int = 8,
        sample_batch_size: int = 8,
    ):
        
        set_all_adapters(
            model = self.ppo_trainer.model,
            enable = True
        )

        dataloader = DataLoader(
            dataset = ds_generator,
            batch_size = sample_batch_size,
            shuffle = True,
            collate_fn = instruct_prompt_collator,
            drop_last = True
        )
        dataloader = self.accelerator.prepare(dataloader)
        max_timestep = len(dataloader) * sample_batch_size * n_episode

        memories = deque([])
        length_sampler = LengthSampler(256, 300)

        timestep = 0
        updatestep = 0
        tqdm_bar = tqdm(
            total = max_timestep // n_update_timestep * n_update_timestep // sample_batch_size,
            disable = not self.accelerator.is_main_process
        )

        self.get_save_timesteps(self.n_save_time, tqdm_bar.total)
        for episode in range(n_episode):
            for prompts_ids, attention_masks, prompt_texts in dataloader:
                
                self.ppo_trainer.model.eval()
                batch_size = len(prompts_ids)
                timestep += batch_size

                (
                    sequences,
                    masks,
                    action_masks
                ) = self.ppo_trainer.generate_batch(
                    prompts_ids = prompts_ids,
                    attention_masks = attention_masks,
                    return_padding = True,
                    **self.generation_config.to_dict()
                )

                rm_rewards = self.get_rm_rewards(
                    reward_model = self.reward_model,
                    sequences = sequences,
                    masks = masks,
                    action_masks = action_masks,
                    verbose = timestep % n_update_timestep == 0
                )
                
                detach_to_cpu_ = lambda t: t.detach().cpu()

                for (
                    sequence,
                    mask,
                    action_mask,
                    rm_reward
                ) in zip(
                    sequences,
                    masks,
                    action_masks,
                    rm_rewards
                ):
                    seq_len = torch.sum(mask).item()
                    memories.append(PPOMemory(*map(detach_to_cpu_, (
                        sequence[: seq_len],
                        mask[: seq_len],
                        action_mask[: seq_len],
                        rm_reward
                    ))))

                if self.clean_cache_every_iter:
                    del (
                        sequences,
                        masks,
                        action_masks,
                        rm_rewards
                    )
                    torch.cuda.empty_cache()
                
                tqdm_bar.update(1)
                if timestep >= (updatestep + 1) * n_update_timestep:
                    updatestep += 1
                    ppo_stats = [self.ppo_trainer.step(memories)]
                    torch.cuda.empty_cache()
                    ppo_stats_gathered = self.accelerator.gather_for_metrics(ppo_stats)
                    if self.accelerator.is_main_process:
                        self.logger.step(
                            episode = episode + 1,
                            timestep = timestep,
                            stat_dict = merge_dict(
                                unmerged_dicts = ppo_stats_gathered,
                                reduce = 'mean'
                            )
                        )

                    while len(memories) > (self.n_sample_reuse - 1) * n_update_timestep:
                        memories.popleft()

                    if self.check_if_save(tqdm_bar.n):
                        if self.accelerator.is_main_process:
                            self.save(
                                self.ppo_trainer.model,
                                os.path.join(self.logger.dir, f'{self.model_name}_{episode}_{timestep}')
                            )

                    if max_timestep - timestep < n_update_timestep:
                        break

        self.logger.info('RLHF Training Complete')
        self.logger.save_res()

def main():

    config = RLHF_Config()

    data_path = os.path.join('/home', 'smliu', 'datasets', 'hf', 'hh-rlhf')
    sub_data_path = ['harmless-base']
    config.dateset_cfg.data_path = data_path
    config.dateset_cfg.sub_data_path = sub_data_path

    model_path = '/home/smliu/huggingface/bit-dny/MindLLM-1b3-chat-zh-v2.0'
    config.model_cfg.peft_cfg.target_modules = ['q_proj', 'k_proj', 'v_proj', 'out_proj']
    config.dateset_cfg.tokenizer_pretrain_path = model_path
    config.model_cfg.model_pretrain_path = model_path
    config.ref_cfg.model_pretrain_path = model_path
    
    rm_path = os.path.join('/home', 'smliu', 'huggingface', 'Ray2333', 'gpt2-large-harmless-reward_model')
    config.reward_cfg.model_pretrain_path = rm_path

    # whole dataset
    # max_sample = 0
    # config.n_update_timestep = 64
    # config.accelertor_cfg.gradient_accumulation_steps = 8
    # config.train_batch_size = 2
    # config.manipulator_cfg.n_adapt_step = 128

    # 1/2 dataset
    # max_sample = 20000
    max_sample = 15000
    config.n_update_timestep = 16
    config.accelertor_cfg.gradient_accumulation_steps = 4
    config.train_batch_size = 2

    config.value_loss_coef = 0.1
    config.n_update_epoch = 2

    if TEST:
        config.accelertor_cfg.gradient_accumulation_steps = 2
        config.n_update_timestep = 8
        config.n_save_step = 1

    config.parse_args()

    trainer = RLHF_Trainer(
        config = config
    )
    
    config.dateset_cfg.tokenize_type = 'prompt_not_pad'
    dataset = Instruct_Pref_Dataset(config.dateset_cfg)
    dataset.load(mode = 'train', max_sample = max_sample)
    trainer.train(
        ds_generator = dataset.get_generator(),
        n_episode = config.n_episode,
        n_update_timestep = config.n_update_timestep,
        sample_batch_size = config.sample_batch_size
    )

if __name__ == '__main__':
    
    main()