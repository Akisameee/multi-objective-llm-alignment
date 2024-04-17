import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import Dataset, DataLoader
from collections import deque, namedtuple
from einops import rearrange, repeat, reduce
from einops.layers.torch import Rearrange
from beartype.typing import List, Optional, Callable, Deque
from functools import partial
from trl import AutoModelForCausalLMWithValueHead
from transformers import AutoTokenizer
from transformers.generation.utils import GenerationConfig
from accelerate import Accelerator
import tqdm
import os

from configs import PPO_Config, model_infos
# from modules.lms import BaseLM, RewardLM
from modules.base import BaseLMWithValueHeads
from modules.utils import masked_mean, ExperienceDataset, shift, log_prob, default, masked_whiten
from logger import Logger

PPOActionCriticReturn = namedtuple(
    'PPOActionCriticReturn',
    [
        'actions',
        'sequence',
        'mask',
        'prompt_mask',
        'action_logits',
        'values'
    ]
)

PPOGenerationReturn = namedtuple(
    'PPOGenerationReturn',
    [
        'sequence',
        'mask',
        'action_mask'
    ]
)

PPOMemory = namedtuple(
    'Memory',
    [
        'sequence',
        'mask',
        'action_mask',
        'prob',
        'logprob',
        'ref_logprob',
        'rm_reward',
        'value'
    ]
)

def create_dataloader(data, batch_size, shuffle = True, device = None, **kwargs):
    ds = ExperienceDataset(data, device = device)
    return DataLoader(ds, batch_size = batch_size, shuffle = shuffle, **kwargs)

# helper functions

def exists(val):
    return val is not None

def masked_normalize(t, eps = 1e-5, mask = None, dim = None):
    dim = default(dim, tuple(range(t.ndim)))
    kwargs = dict(dim = dim, keepdim = True)

    mean = masked_mean(t, mask = mask, **kwargs)
    mean_centered = t - mean
    var = masked_mean(mean_centered ** 2, mask = mask, **kwargs)

    return mean_centered * var.clamp(min = eps).rsqrt()

def pad_sequence_fixed(sequences, *args, **kwargs):
    first_el = sequences[0]
    has_no_dimension = first_el.ndim == 0

    if has_no_dimension:
        sequences = tuple(map(lambda t: t[None], sequences))

    out = pad_sequence(sequences, *args, **kwargs)

    if has_no_dimension:
        out = rearrange(out, '... 1 -> ...')

    return out

def log(t, eps = 1e-20):
    return torch.log(t.clamp(min = eps))

def masked_entropy(prob, dim = -1, mask = None):
    entropies = (prob * log(prob)).sum(dim = -1)
    return masked_mean(entropies, mask = mask).mean()

def masked_kl_div(prob1, prob2, mask = None, reduce_batch = False):
    """
    need to account for variable sequence lengths, therefore not using the built-in functional version
    """
    kl_divs = (prob1 * (log(prob1) - log(prob2))).sum(dim = -1)
    loss = masked_mean(kl_divs, mask)

    if reduce_batch:
        return loss.mean()

    return loss
    

class PPO_Trainer(nn.Module):

    def __init__(
        self,
        config: PPO_Config,
        model: AutoModelForCausalLMWithValueHead | BaseLMWithValueHeads,
        tokenizer: AutoTokenizer = None,
        optimizer: torch.optim.Optimizer = None,
        accelerator: Accelerator = None,
        logger: Logger = None
    ):
        super().__init__()

        # models
        self.model = model
        self.model_info = config.model_cfg.model_info
        self.uni_info = model_infos['universal']
        self.model_params = filter(lambda p: p.requires_grad, self.model.parameters())

        self.tokenizer = tokenizer
        if self.tokenizer is None:
            self.tokenizer = AutoTokenizer.from_pretrained(config.model_cfg.model_pretrain_path, padding_side = 'left')
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

        # optimizers
        self.optimizer = optimizer
        if self.optimizer is None:
            self.optimizer = torch.optim.AdamW(
                filter(lambda p: p.requires_grad, self.model.parameters()),
                lr = config.lr,
                weight_decay = config.weight_decay
            )

        # accelerator
        self.accelerator = accelerator

        self.logger = logger
        if self.logger is None:
            self.logger = Logger(
                output_dir = config.output_dir,
                task_name = 'RLHF_train',
                disable = not self.accelerator.is_main_process
            )

        # train hyperparams
        self.n_update_epoch = config.n_update_epoch
        self.train_batch_size = config.train_batch_size
        self.max_norm = config.max_norm
        self.kl_type = config.kl_type
        self.kl_ref_coef = config.kl_ref_coef

        # ppo hyperparams
        self.eps_clip = config.eps_clip
        self.value_clip = config.value_clip
        self.beta_s = config.beta_s
        self.lam = config.lam
        self.gae_gamma = config.gae_gamma
        self.ratio_threshold = config.ratio_threshold
        self.value_loss_coef = config.value_loss_coef

        self.ckpt_path = config.model_cfg.ckpt_path
    
    @property
    def device(self):
        if self.accelerator is not None and not getattr(self.model, "is_sequential_parallel", False):
            return self.accelerator.device
        else:
            return self.model.device
        
    # def replace_instruct_prompts(
    #     self,
    #     target_str: str
    # ):
    #     target_str = target_str.replace(self.model_info['prompt_prefix'], self.uni_info['prompt_prefix'])
    #     target_str = target_str.replace(self.model_info['response_prefix'], self.uni_info['response_prefix'])

    #     return target_str
        
    # def decode_single(self, input_ids, attention_mask, prompt_mask):

    #     input_ids = input_ids.squeeze()
    #     attention_mask = attention_mask.squeeze()
    #     prompt_mask = prompt_mask.squeeze()

    #     prompt_ids = input_ids[:torch.sum(prompt_mask)]
    #     response_ids = input_ids[torch.sum(prompt_mask): torch.sum(attention_mask)]

    #     prompt = self.tokenizer.decode(prompt_ids, skip_special_tokens = True)
    #     response = self.tokenizer.decode(response_ids, skip_special_tokens = True)
    #     prompt = self.replace_instruct_prompts(prompt)
    #     response = self.replace_instruct_prompts(response)

    #     return prompt, response
    
    # def decode_batch(self, inputs_ids, attention_masks, prompt_masks):

    #     prompts = []
    #     responses = []
    #     for input_ids, mask, prompt_mask in zip(inputs_ids, attention_masks, prompt_masks):
            
    #         prompt, response = self.decode_single(input_ids, mask, prompt_mask)
    #         prompts.append(prompt)
    #         responses.append(response)
        
    #     return prompts, responses

    def decode_batch(self, inputs_ids, attention_masks, prompt_masks):

        return self.accelerator.unwrap_model(self.model).decode_batch(inputs_ids, attention_masks, prompt_masks)
    
    @torch.no_grad()
    def generate_batch(
        self,
        prompts_ids: torch.FloatTensor,
        attention_masks: torch.LongTensor,
        return_padding: bool = False,
        use_max_length: bool = True,
        **kwargs
    ):
        
        if kwargs['eos_token_id'] is None:
            eos_token_id = self.tokenizer.eos_token_id
        else:
            eos_token_id = kwargs['eos_token_id']
        kwargs['pad_token_id'] = self.tokenizer.pad_token_id
        
        if use_max_length:
            kwargs.pop('max_new_tokens')
        else:
            kwargs.pop('max_length')
            if kwargs['max_new_tokens'] is None:
                kwargs['max_new_tokens'] = 256

        inputs = {
            'input_ids': prompts_ids,
            'attention_mask': attention_masks
        }
        inputs_pad = self.tokenizer.pad(
            inputs,
            padding = True,
            max_length = None,
            return_tensors = 'pt',
        ).to(self.accelerator.device)
        input_len = inputs_pad['input_ids'].shape[-1]

        # kwargs.pop('generation_kwargs')
        if self.accelerator is not None:
            sequences = self.accelerator.unwrap_model(self.model).generate(
                **inputs_pad,
                **kwargs
            )
        else:
            sequences = self.model.generate(
                **inputs_pad,
                **kwargs
            )

        sequences_return = []
        masks_return = []
        action_masks_return = []
        for sequence, mask in zip(sequences, inputs_pad['attention_mask']):

            responses = sequence[input_len:]
            if eos_token_id in responses:
                pad_mask = responses == eos_token_id
                post_pad_idx = torch.nonzero(pad_mask, as_tuple = False)[0, 0].item() + 1
            else:
                post_pad_idx = responses.shape[0]
            post_pad_idx += input_len

            sequence_return = sequence[(1 - mask).sum(): post_pad_idx]
            mask_return = torch.ones_like(sequence_return)
            action_mask_return = torch.ones_like(sequence_return)
            action_mask_return[: mask.sum()] = 0
            # print(self.tokenizer.decode(sequence_return))
            # print(self.tokenizer.decode(sequence_return * mask_return))
            # print(self.tokenizer.decode(sequence_return * action_mask_return))
            sequences_return.append(sequence_return)
            masks_return.append(mask_return)
            action_masks_return.append(action_mask_return)

        if return_padding:
            sequences_return = pad_sequence(
                sequences_return,
                batch_first = True,
                padding_value = self.tokenizer.pad_token_id
            )
            masks_return = pad_sequence(
                masks_return,
                batch_first = True,
                padding_value = 0
            )
            action_masks_return = pad_sequence(
                action_masks_return,
                batch_first = True,
                padding_value = 0
            )

        return PPOGenerationReturn(
            sequences_return,
            masks_return,
            action_masks_return
        )
        
    @torch.no_grad()
    def generate(
            self,
            prompt_ids: torch.FloatTensor,
            max_seq_len: int,
            eos_token: int = None,
            **kwargs
        ):
        kwargs.pop('max_length')

        if self.accelerator is not None:
            sequence = self.accelerator.unwrap_model(self.model).generate(
                # max_seq_len,
                input_ids = prompt_ids,
                **kwargs
            )
        else:
            sequence = self.model.generate(
                input_ids = prompt_ids,
                **kwargs
            )

        # print(self.tokenizer.decode(sequence.squeeze()))

        prompt_len = prompt_ids.shape[-1]
        action_len = sequence.shape[-1] - prompt_len
        actions = sequence[..., prompt_len:]
        
        prompt_mask = torch.arange(sequence.shape[-1], device = prompt_ids.device) < prompt_len
        prompt_mask = repeat(prompt_mask, 'n -> b n', b = sequence.shape[0])

        action_mask = ~prompt_mask

        mask = None
        if eos_token is not None:
            mask = ((sequence == eos_token).cumsum(dim = -1) == 0)
            mask = F.pad(mask, (1, -1), value = True) # include eos token
            action_mask &= mask

        action_logits, value = self.batch_forward(
            sequence
            # mask = action_mask
        )

        return PPOActionCriticReturn(
            actions,
            sequence,
            mask,
            prompt_mask,
            action_logits,
            value
        )
    
    def batch_forward(
        self,
        x: torch.FloatTensor,
        mask: torch.LongTensor = None
    ):
        
        action_logits, _, values = self.model(
            input_ids = x,
            attention_mask = mask
        )

        return action_logits, values

    def learn(
        self,
        memories: Deque[PPOMemory]
    ):
        # stack all data stored in the memories
        (
            sequences,
            masks,
            all_action_masks,
            old_probs,
            old_logprobs,
            ref_logprobs,
            rm_rewards,
            old_values
        ) = list(map(partial(pad_sequence_fixed, batch_first = True), zip(*memories)))

        rewards, kl_refs = self.compute_rewards(
            rm_rewards = rm_rewards,
            logprobs = old_logprobs,
            ref_logprobs = ref_logprobs,
            masks = all_action_masks
        )

        old_values = shift(old_values, shift = 1, dim = -1)
        old_values, advantages, returns = self.compute_advantages(
            old_values,
            rewards,
            all_action_masks
        )

        # prepare dataloader
        dataloader = DataLoader(
            ExperienceDataset(
                [
                    sequences,
                    masks,
                    all_action_masks,
                    old_logprobs,
                    old_values,
                    returns,
                    advantages,
                ],
                # device = self.device
            ),
            batch_size = self.train_batch_size,
            shuffle = True
        )
        dataloader = self.accelerator.prepare(dataloader)

        self.model.train()

        # PPO training
        policy_losses = []
        critic_losses = []
        for _ in range(self.n_update_epoch):
            for (
                sequences,
                masks,
                action_masks,
                old_logprobs,
                old_values,
                returns,
                advantages,
            ) in dataloader:
                
                logits, values = self.batch_forward(
                    sequences,
                    mask = masks
                )

                logits = shift(logits, shift = 1, dim = -2)
                values = shift(values, shift = 1, dim = -1)

                probs = logits.softmax(dim = -1)
                logprobs = log_prob(probs, sequences)

                policy_loss, value_loss = self.loss(
                    logprobs,
                    old_logprobs,
                    advantages,
                    returns,
                    action_masks,
                    values,
                    old_values
                )
                critic_losses.append(value_loss.item())
                policy_losses.append(policy_loss.item())

                # combine losses
                loss = policy_loss + value_loss

                # update
                self.accelerator.backward(loss)

                if self.max_norm is not None:
                    if self.accelerator is not None:
                        if self.accelerator.sync_gradients:
                            self.accelerator.clip_grad_norm_(self.model_params, self.max_norm)
                    else:
                        torch.nn.utils.clip_grad_norm_(self.model_params, self.max_norm)

                self.optimizer.step()
                self.optimizer.zero_grad()

        ppo_stats = self.get_train_stats(
            masks = all_action_masks,
            policy_losses = policy_losses,
            critic_losses = critic_losses,
            rewards = rewards,
            rm_rewards = rm_rewards,
            kl_refs = kl_refs
        )
        
        return ppo_stats
    
    def kl_penalty(
        self,
        logprob: torch.FloatTensor,
        ref_logprob: torch.FloatTensor
    ) -> torch.FloatTensor:
    
        if self.kl_type == "kl":
            return logprob - ref_logprob

        if self.kl_type == "abs":
            return torch.abs(logprob - ref_logprob)

        if self.kl_type == "mse":
            return 0.5 * torch.square(logprob - ref_logprob)

        if self.kl_type == "full":
            return F.kl_div(ref_logprob, logprob, log_target=True, reduction="none").sum(-1)

    def compute_rewards(
        self,
        rm_rewards: torch.FloatTensor,
        logprobs: torch.FloatTensor,
        ref_logprobs: torch.FloatTensor,
        masks: torch.LongTensor
    ):
        
        kl_refs = []
        rewards = []
        for rm_reward, logprob, ref_logprob, mask in zip(rm_rewards, logprobs, ref_logprobs, masks):
            kl_ref = -self.kl_penalty(logprob, ref_logprob) * self.kl_ref_coef
            reward = kl_ref.clone()
            reward[mask.nonzero()[-1]] += rm_reward
            kl_refs.append(kl_ref)
            rewards.append(reward)
        
        return torch.stack(rewards), torch.stack(kl_refs)
    
    def loss(
        self,
        logprobs: torch.FloatTensor,
        old_logprobs: torch.FloatTensor,
        advantages: torch.FloatTensor,
        returns: torch.FloatTensor,
        masks: torch.LongTensor,
        values: torch.FloatTensor,
        old_values: torch.FloatTensor
    ):
        
        # calculate policy loss
        ratios = torch.exp(logprobs - old_logprobs)
        ploicy_surr_1 = -ratios * advantages
        policy_surr_2 = -torch.clamp(ratios, 1 - self.eps_clip, 1 + self.eps_clip) * advantages
        policy_loss = masked_mean(torch.max(ploicy_surr_1, policy_surr_2), mask = masks)

        # calculate value loss
        value_clipped = old_values + (values - old_values).clamp(-self.value_clip, self.value_clip)
        value_loss_1 = (old_values - returns) ** 2
        value_loss_2 = (value_clipped - returns) ** 2
        value_loss = 0.5 * masked_mean(torch.max(value_loss_1, value_loss_2), mask = masks)

        avg_ratio = masked_mean(ratios, dim = None).item()
        if avg_ratio > self.ratio_threshold:
            policy_loss = policy_loss * 0.0
            value_loss = value_loss * 0.0
            if self.accelerator.is_main_process:
                self.logger.info(f'Current Ratio({avg_ratio:.4g}) > Threshold({self.ratio_threshold}) Skipping Batch')

        return policy_loss, self.value_loss_coef * value_loss

    def compute_advantages(
        self,
        values: torch.FloatTensor,
        rewards: torch.FloatTensor,
        mask: torch.FloatTensor,
    ):
        lastgaelam = 0
        advantages_reversed = []
        gen_len = rewards.shape[-1]

        values = values * mask
        rewards = rewards * mask

        for t in reversed(range(gen_len)):
            nextvalues = values[:, t + 1] if t < gen_len - 1 else 0.0
            delta = rewards[:, t] + self.gae_gamma * nextvalues - values[:, t]
            lastgaelam = delta + self.gae_gamma * self.lam * lastgaelam
            advantages_reversed.append(lastgaelam)
        advantages = torch.stack(advantages_reversed[::-1]).transpose(0, 1)

        returns = advantages + values
        advantages = masked_whiten(advantages, mask)
        advantages = advantages.detach()

        return values, advantages, returns
    
    def get_train_stats(self, masks, **train_records):

        policy_losses = train_records.pop('policy_losses')
        policy_loss_mean = sum(policy_losses) / len(policy_losses)

        critic_losses = train_records.pop('critic_losses')
        critic_loss_mean = sum(critic_losses) / len(critic_losses)

        rewards = train_records.pop('rewards')
        reward_mean = masked_mean(rewards, mask = masks, dim = None).item()
        kl_refs = train_records.pop('kl_refs')
        kl_ref_mean = masked_mean(kl_refs, mask = masks, dim = None).item()
        rm_rewards = train_records.pop('rm_rewards')
        rm_reward_mean = torch.mean(rm_rewards).item()
        reward_total = kl_ref_mean + rm_reward_mean

        response_len_mean = torch.sum(masks).item() / masks.shape[0]

        train_stats = {
            'policy_loss': policy_loss_mean,
            'critic_loss': critic_loss_mean,
            'rm_score': rm_reward_mean,
            'ref_kl': kl_ref_mean,
            'generate_length': response_len_mean
        }

        return train_stats
    
    def catch_cuda_error(func):
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except RuntimeError:
                if 'logger' in kwargs.keys():
                    kwargs['logger'].warning('CUDA RuntimeError, skipping batch.')
                torch.cuda.empty_cache()
        return wrapper
