main_path=/home/smliu/RLHF
launch_path=$main_path/train_morlhf.py

model_path=/home/share/models/huggingface/bit-dny/MindLLM

cd $main_path

CUDA_VISIBLE_DEVICES=0,1,7 \
accelerate launch --num_processes=3 $launch_path \
# python $launch_path \
#     --sample_batch_size 1 \
#     --dateset_cfg_tokenizer_pretrain_path $model_path \
#     --model_cfg_model_pretrain_path $model_path \
#     --ref_cfg_model_pretrain_path $model_path \