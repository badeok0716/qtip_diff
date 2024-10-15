import argparse
import os
import time

import glog

os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'max_split_size_mb:512'

import torch
import torch.multiprocessing as mp
from transformers import AutoModelForCausalLM, AutoTokenizer
from transformers.modeling_attn_mask_utils import \
    _prepare_4d_causal_attention_mask

from lib import utils
from lib.algo import finetune
from lib.codebook import bitshift

parser = argparse.ArgumentParser()
parser.add_argument('--seed', default=0, type=int)
parser.add_argument('--num_cpu_threads', default=8, type=int)
parser.add_argument('--batch_size', default=16, type=int)
parser.add_argument('--devset_size', default=384, type=int)
parser.add_argument('--ctx_size', default=4096, type=int)
parser.add_argument('--save_path', type=str)
parser.add_argument('--in_hess_path', type=str)
parser.add_argument('--base_model', type=str)
parser.add_argument('--sigma_reg', default=1e-2, type=float)
parser.add_argument('--sigma_reg2', default=1e-2, type=float)
parser.add_argument('--scale_override', default=-1, type=float)
parser.add_argument('--codebook', type=str)
parser.add_argument('--use_fp64', action='store_true')
parser.add_argument('--no_use_buffered', action='store_true')
parser.add_argument('--sample_proc', default=1, type=int)
parser.add_argument('--lowmem_ldlq', action='store_true')
parser.add_argument('--ft_lr', default=3e-6, type=float)
parser.add_argument('--ft_bs', default=4, type=int)
parser.add_argument('--ft_update_freq', default=1, type=int)
parser.add_argument('--ft_epochs', default=5, type=int)
parser.add_argument('--ft_valid_freq', default=1, type=int)
parser.add_argument('--ft_valid_size', default=128, type=float)
parser.add_argument('--ft_early_stop', default=5, type=int)
parser.add_argument('--ft_grad_ckpt', action='store_true')
parser.add_argument('--td_x', default=16, type=int)
parser.add_argument('--td_y', default=16, type=int)
parser.add_argument('--L', default=16, type=int)
parser.add_argument('--K', default=2, type=int)
parser.add_argument('--V', default=2, type=int)
parser.add_argument('--tlut_bits', default=0, type=int)
parser.add_argument('--decode_mode', default='lut', type=str)
parser.add_argument('--ft_train_lut', action='store_true')


def check_exist(idx, args):
    suffix = ['q', 'k', 'v', 'o', 'up', 'down', 'layernorm']
    for _ in suffix:
        test = f'{args.save_path}/{idx}_{_}.pt'
        if not os.path.exists(test):
            return False
    return True


def quantize_llama_decoder(layer, idx, cb, args, device, pre_orig_emb,
                           orig_emb, model_config):
    if check_exist(idx, args):
        return

    # layer name, save_name, input hessian file, output hessian file
    quant_order = [('self_attn.v_proj', 'v', 'qkv', 'v'),
                   ('self_attn.q_proj', 'q', 'qkv', 'q'),
                   ('self_attn.k_proj', 'k', 'qkv', 'k'),
                   ('self_attn.o_proj', 'o', 'o', 'o'),
                   ('mlp.up_proj', 'up', 'up', 'up'),
                   ('mlp.gate_proj', 'gate', 'up', 'gate'),
                   ('mlp.down_proj', 'down', 'down', 'down')]
    finetune.quantize_finetune_decoder_layer(layer, quant_order, idx, cb, args,
                                             device, pre_orig_emb, orig_emb)
    torch.save(
        {
            'input_layernorm': layer.input_layernorm.weight,
            'post_attention_layernorm': layer.post_attention_layernorm.weight,
        }, f'{args.save_path}/{idx}_layernorm.pt')


def main(args):
    dtype_ = torch.float64 if args.use_fp64 else torch.float32

    cb = bitshift.bitshift_codebook(L=args.L,
                                    K=args.K,
                                    V=args.V,
                                    tlut_bits=args.tlut_bits,
                                    decode_mode=args.decode_mode)
    model = AutoModelForCausalLM.from_pretrained(args.base_model,
                                                 torch_dtype='auto',
                                                 low_cpu_mem_usage=True)

    # save configs
    all_config = {'quant_args': args, 'model_config': model.config}
    quip_params = {
        'codebook': args.codebook,
        'codebook_version': cb.version,
        'L': args.L,
        'K': args.K,
        'V': args.V,
        'tlut_bits': args.tlut_bits,
        'decode_mode': args.decode_mode,
        'td_x': args.td_x,
        'td_y': args.td_y,
    }
    all_config['model_config'].update({'quip_params': quip_params})
    torch.save(all_config, os.path.join(args.save_path, 'config.pt'))

    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    tokenizer.pad_token = tokenizer.eos_token
    glog.info('loaded model')

    devset = utils.sample_rp1t(tokenizer, args.devset_size, args.ctx_size,
                               args.sample_proc)
    glog.info('loaded dataset and devset')

    nproc = torch.cuda.device_count()
    orig_emb_cache = [model.model.embed_tokens(devset)]
    for _ in range(nproc):
        orig_emb_cache.append(
            torch.zeros(orig_emb_cache[0].shape,
                        dtype=orig_emb_cache[0].dtype,
                        device=orig_emb_cache[0].device))

    position_ids = torch.arange(args.ctx_size, dtype=torch.int32)[None, :] + \
        torch.zeros(args.batch_size, args.ctx_size, dtype=torch.int32)
    attention_mask = _prepare_4d_causal_attention_mask(
        None, (args.batch_size, args.ctx_size),
        orig_emb_cache[0][:args.batch_size], 0)

    cur_device = 0
    proc_list = [None for _ in range(nproc)]
    for i in range(len(model.model.layers)):
        glog.info(f'layer {i} gpu {cur_device}')
        if proc_list[cur_device] is not None:
            proc_list[cur_device][0].join()
            model.model.layers[proc_list[cur_device][1]] = None
            utils.clean()
            if cur_device == 0:
                orig_emb_cache[0].copy_(orig_emb_cache[-1])
        if cur_device + 1 < nproc and proc_list[cur_device + 1] is not None:
            proc_list[cur_device + 1][0].join()
        utils.clean()
        st = time.time()
        position_ids = position_ids.to(cur_device)
        attention_mask = attention_mask.to(cur_device)
        print(position_ids.numel(), attention_mask.numel())
        model.model.layers[i].to(cur_device)
        for j in range(args.devset_size // args.batch_size):
            utils.clean()
            orig_emb_cache[cur_device + 1][args.batch_size * j : args.batch_size * (j + 1)] = \
                model.model.layers[i](
                    orig_emb_cache[cur_device][args.batch_size * j : args.batch_size * (j + 1)].to(cur_device),
                    position_ids=position_ids,
                    attention_mask=attention_mask,
                    use_cache=False,
                    output_attentions=False)[0].cpu()
        model.model.layers[i].cpu()
        orig_msv = orig_emb_cache[cur_device].float().norm(
        )**2 / orig_emb_cache[cur_device].numel()
        target_msv = orig_emb_cache[cur_device + 1].float().norm(
        )**2 / orig_emb_cache[cur_device + 1].numel()
        position_ids = position_ids.cpu()
        attention_mask = attention_mask.cpu()
        utils.clean()
        glog.info(
            'computed original embedding for layer {} in {}s, pre msv {}, post msv {}'
            .format(i,
                    time.time() - st, orig_msv, target_msv))

        proc_list[cur_device] = (mp.Process(target=quantize_llama_decoder,
                                            args=(
                                                model.model.layers[i],
                                                i,
                                                cb,
                                                args,
                                                cur_device,
                                                orig_emb_cache[cur_device],
                                                orig_emb_cache[cur_device + 1],
                                                all_config['model_config'],
                                            )), i)
        proc_list[cur_device][0].start()

        cur_device = (cur_device + 1) % nproc

    for p in proc_list:
        p[0].join()


if __name__ == '__main__':
    torch.set_grad_enabled(False)
    mp.set_start_method('spawn')
    mp.set_sharing_strategy('file_system')
    args = parser.parse_args()
    torch.manual_seed(args.seed)
    os.makedirs(args.save_path, exist_ok=True)
    main(args)
