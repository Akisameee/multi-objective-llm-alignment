import torch
from torch.utils.data import Dataset
import os
import csv
import re
from transformers import AutoTokenizer, AutoConfig

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datas.dataset_parser import Dataset_Parser
from configs import SFT_Train_Config, Instruct_Dataset_Config, RLHF_Config
from configs import dataset_infos, model_infos


csv.field_size_limit(10000000)
TEST = 1000

def instruct_prompt_collator(batch):

    input_ids = [data[0].squeeze() for data in batch]
    attention_masks = [data[1].squeeze() for data in batch]
    prompt_texts = [data[2] for data in batch]

    return input_ids, attention_masks, prompt_texts

def has_chinese(str_check):

    for char in str_check:
        if '\u4e00' <= char <= '\u9fa5':
            return True
    return False

class Generator_Dataset(Dataset):

    def __init__(self, datas):
        
        self.datas = datas

    def __getitem__(self, index):

        return tuple(map(lambda t: t[index], self.datas))
    
    def __len__(self):

        return len(self.datas[0])

class Instruct_Pref_Dataset():

    def __init__(
            self,
            config: Instruct_Dataset_Config
        ):
        
        self.tokenizer = AutoTokenizer.from_pretrained(config.tokenizer_pretrain_path, padding_side = config.padding_side)
        if not self.tokenizer.pad_token:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.max_len = config.max_len

        self.dataset_parser = Dataset_Parser(config = config)
        self.model_info = self.dataset_parser.model_info
        self.tokenizer_kwargs = self.model_info['tokenizer_kwargs'] if 'tokenizer_kwargs' in self.model_info.keys() else {}
        # self.data_path = config.data_path
        # self.sub_data_path = config.sub_data_path
        self.prompt_only = True if config.tokenize_type in ['prompt_pad', 'prompt_not_pad'] else False
        self.tokenize = {
            'prompt_pad': self.tokenize_prompt_pad,
            'prompt_not_pad': self.tokenize_prompt_not_pad,
            'prompt_response': self.tokenize_prompt_response
        }[config.tokenize_type]
        
        self.texts = []
        self.datas = []

    def load(
        self,
        mode = 'train',
        max_sample = None
    ):
        texts = self.dataset_parser.parse_dataset(mode = mode, max_sample = max_sample)
        self.texts += texts
        self.datas += self.tokenize_parsed_texts(texts)
        
    def tokenize_prompt_pad(self, prompt_texts, response_texts):
        
        tokenizer_out = self.tokenizer.batch_encode_plus(
            prompt_texts,
            padding = True,
            truncation = True,
            max_length = self.max_len,
            return_tensors = 'pt',
            return_token_type_ids = False,
            **self.tokenizer_kwargs
        )
        input_ids = tokenizer_out['input_ids']
        attention_mask = tokenizer_out['attention_mask']

        return [input_ids, attention_mask, prompt_texts]
    
    def tokenize_prompt_not_pad(self, prompt_texts, response_texts):

        input_ids = []
        attention_mask = []
        prompt_texts_new = []
        for prompt_text in prompt_texts:
            tokenizer_out = self.tokenizer.encode_plus(
                prompt_text,
                padding = 'do_not_pad',
                truncation = True,
                max_length = self.max_len,
                return_tensors = 'pt',
                return_token_type_ids = False,
                **self.tokenizer_kwargs
            )
            if tokenizer_out['input_ids'].size(-1) > 0.5 * self.max_len:
                continue
            input_ids.append(tokenizer_out['input_ids'])
            attention_mask.append(tokenizer_out['attention_mask'])
            prompt_texts_new.append(prompt_text)

        return [input_ids, attention_mask, prompt_texts_new]
    
    def tokenize_prompt_response(self, prompt_texts, response_texts):

        datas = []
        for prompt_text, response_text in zip(prompt_texts, response_texts):
            tokenizer_out = self.tokenizer.encode_plus(
                prompt_text,
                response_text,
                padding = True,
                truncation = True,
                max_length = self.max_len,
                return_tensors = 'pt',
                return_token_type_ids = True,
                **self.tokenizer_kwargs
            )
            tokenizer_out = self.get_response_label(tokenizer_out)
            tokenizer_out['prompt_text'] = prompt_text
            tokenizer_out['response_text'] = response_text
            datas.append(tokenizer_out)

        return datas

    def get_response_label(self, tokenizer_out):

        prompt_len = torch.sum(tokenizer_out['token_type_ids'])
        tokenizer_out['labels'] = torch.where(
            tokenizer_out['input_ids'] == self.tokenizer.pad_token_id,
            -100,
            tokenizer_out['input_ids']
        )
        tokenizer_out['labels'][:, :prompt_len] = -100

        return tokenizer_out
        
    def read_sharegpt_dataset(self, data_path):
        
        prompt_texts = []
        response_texts = []
        prompt_pattern = r'<s>Human: (.*?)</s>'
        response_pattern = r'<s>Assistant: (.*?)</s>'

        with open(data_path, 'r', encoding='utf-8') as file:
            for index, line in enumerate(csv.reader(file)):
                if index == 0:
                    continue
                line = line[0].strip('\n')

                prompt_text = re.findall(prompt_pattern, line, re.DOTALL)[0]
                response_text = re.findall(response_pattern, line, re.DOTALL)[0]
                (
                    prompt_text,
                    response_text
                ) = self.add_instruct_prompt(
                    prompt_text,
                    response_text
                )

                if len(prompt_text) <= 0 or len(response_text) <= 0:
                    continue

                if self.remove_chinese:
                    if has_chinese(prompt_text):
                        continue
                
                prompt_texts.append(prompt_text)
                response_texts.append(response_text)
                if len(prompt_texts) == TEST:
                    break
        
        if len(prompt_texts) > 0:
            datas = self.tokenize(prompt_texts, response_texts)
        else:
            datas = None
        return datas
    
    def tokenize_parsed_texts(self, texts):

        prompt_texts = [text['prompt'] for text in texts]
        response_key = 'response' if 'response' in texts[0].keys() else 'chosen'
        response_texts = [text[response_key] for text in texts]
        datas = self.tokenize(prompt_texts, response_texts)

        return datas

    def get_generator(self):

        return Generator_Dataset(self.datas)

if __name__ == '__main__':

    config = RLHF_Config()

    data_path = os.path.join('/home', 'smliu', 'datasets', 'hf', 'hh-rlhf')
    sub_data_path = ['helpful-base']
    config.dateset_cfg.data_path = data_path
    config.dateset_cfg.sub_data_path = sub_data_path

    model_path = '/home/smliu/huggingface/bit-dny/MindLLM-1b3-chat-zh-v2.0'
    # model_path = '/home/share/models/huggingface/meta-llama/Llama-2-7b-chat-hf'
    config.dateset_cfg.tokenizer_pretrain_path = model_path
    config.model_cfg.model_pretrain_path = model_path
    config.ref_cfg.model_pretrain_path = model_path
    dataset = Instruct_Pref_Dataset(config.dateset_cfg)
    dataset.load(
        mode = 'train'
    )
    instruct_ds = dataset.get_generator()
    loader = torch.utils.data.DataLoader(instruct_ds, batch_size = 8, collate_fn = instruct_prompt_collator, shuffle = True)
    for batch in loader:
        print('Decode', dataset.tokenizer.decode(batch[0][0]))
        print('Prompt',batch[2][0])
        print('-------')
        # print(batch[0], batch[1])
    print(dataset.datas)
