from typing import *

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

import torch.linalg as linalg

from tqdm import tqdm


def default(val, d):
    return val if val is not None else d


def make_sparse(t: torch.Tensor, sparsity=0.95):
    abs_t = torch.abs(t)
    np_array = abs_t.detach().cpu().numpy()
    quan = float(np.quantile(np_array, sparsity))
    sparse_t = t.masked_fill(abs_t < quan, 0)
    return sparse_t


@torch.no_grad()
def extract_conv(
    weight: Union[torch.Tensor, nn.Parameter],
    mode = 'fixed',
    mode_param = 0,
    device = 'cpu',
    is_cp = False,
) -> Tuple[nn.Parameter, nn.Parameter]:
    weight = weight.float().to(device)
    out_ch, in_ch, kernel_size, _ = weight.shape
    
    U, S, Vh = linalg.svd(weight.reshape(out_ch, -1))
    
    if mode=='fixed':
        lora_rank = mode_param
    elif mode=='threshold':
        assert mode_param>=0
        lora_rank = torch.sum(S>mode_param)
    elif mode=='ratio':
        assert 1>=mode_param>=0
        min_s = torch.max(S)*mode_param
        lora_rank = torch.sum(S>min_s)
    elif mode=='quantile' or mode=='percentile':
        assert 1>=mode_param>=0
        s_cum = torch.cumsum(S, dim=0)
        min_cum_sum = mode_param * torch.sum(S)
        lora_rank = torch.sum(s_cum<min_cum_sum)
    else:
        raise NotImplementedError('Extract mode should be "fixed", "threshold", "ratio" or "quantile"')
    lora_rank = max(1, lora_rank)
    lora_rank = min(out_ch, in_ch, lora_rank)
    if lora_rank>=out_ch/2 and not is_cp:
        return weight.cpu(), 'full'
    
    U = U[:, :lora_rank]
    S = S[:lora_rank]
    U = U @ torch.diag(S)
    Vh = Vh[:lora_rank, :]
    
    diff = (weight - (U @ Vh).reshape(out_ch, in_ch, kernel_size, kernel_size)).detach().to(dtype=weight.dtype).cpu()
    extract_weight_A = Vh.reshape(lora_rank, in_ch, kernel_size, kernel_size).detach().to(dtype=weight.dtype).cpu()
    extract_weight_B = U.reshape(out_ch, lora_rank, 1, 1).detach().to(dtype=weight.dtype).cpu()
    del U, S, Vh, weight
    return (extract_weight_A, extract_weight_B, diff), 'low rank'


@torch.no_grad()
def extract_linear(
    weight: Union[torch.Tensor, nn.Parameter],
    mode = 'fixed',
    mode_param = 0,
    device = 'cpu',
) -> Tuple[nn.Parameter, nn.Parameter]:
    weight = weight.float().to(device)
    out_ch, in_ch = weight.shape
    
    U, S, Vh = linalg.svd(weight)
    
    if mode=='fixed':
        lora_rank = mode_param
    elif mode=='threshold':
        assert mode_param>=0
        lora_rank = torch.sum(S>mode_param)
    elif mode=='ratio':
        assert 1>=mode_param>=0
        min_s = torch.max(S)*mode_param
        lora_rank = torch.sum(S>min_s)
    elif mode=='quantile' or mode=='percentile':
        assert 1>=mode_param>=0
        s_cum = torch.cumsum(S, dim=0)
        min_cum_sum = mode_param * torch.sum(S)
        lora_rank = torch.sum(s_cum<min_cum_sum)
    else:
        raise NotImplementedError('Extract mode should be "fixed", "threshold", "ratio" or "quantile"')
    lora_rank = max(1, lora_rank)
    lora_rank = min(out_ch, in_ch, lora_rank)
    if lora_rank>=out_ch/2:
        return weight.cpu(), 'full'
    
    U = U[:, :lora_rank]
    S = S[:lora_rank]
    U = U @ torch.diag(S)
    Vh = Vh[:lora_rank, :]
    
    diff = (weight - U @ Vh).detach().to(dtype=weight.dtype).cpu()
    extract_weight_A = Vh.reshape(lora_rank, in_ch).detach().to(dtype=weight.dtype).cpu()
    extract_weight_B = U.reshape(out_ch, lora_rank).detach().to(dtype=weight.dtype).cpu()
    del U, S, Vh, weight
    return (extract_weight_A, extract_weight_B, diff), 'low rank'


def extract_diff(
    base_model,
    db_model,
    mode = 'fixed',
    linear_mode_param = 0,
    conv_mode_param = 0,
    extract_device = 'cpu',
    use_bias = False,
    sparsity = 0.98,
    small_conv = True,
    min_diff = 0.0,
):
    UNET_TARGET_REPLACE_MODULE = [
        "Transformer2DModel", 
        "Attention", 
        "ResnetBlock2D", 
        "Downsample2D", 
        "Upsample2D"
    ]
    UNET_TARGET_REPLACE_NAME = [
        "conv_in",
        "conv_out",
        "time_embedding.linear_1",
        "time_embedding.linear_2",
    ]
    TEXT_ENCODER_TARGET_REPLACE_MODULE = ["CLIPAttention", "CLIPMLP"]
    LORA_PREFIX_UNET = 'lora_unet'
    LORA_PREFIX_TEXT_ENCODER = 'lora_te'

    LORA_PREFIX_TEXT_ENCODER1 = 'lora_te1'
    LORA_PREFIX_TEXT_ENCODER2 = 'lora_te2'

    def extract_lora(prefix, module, child_module, name, child_name, loras, min_diff):
        lora_name = prefix + '.' + name + (('.' + child_name) if child_name != '' else '')
        lora_name = lora_name.replace('.', '_')
        layer = module.__class__.__name__

        root_weight = module.weight
        if torch.allclose(root_weight, child_module.weight):
            return

        diff = root_weight - child_module.weight
        if min_diff > 0.0 and min_diff > torch.max(torch.abs(diff)):
            return

        if layer == 'Linear':
            weight, decompose_mode = extract_linear(
                diff,
                mode,
                linear_mode_param,
                device = extract_device,
            )
            if decompose_mode == 'low rank':
                extract_a, extract_b, diff = weight
        elif layer == 'Conv2d':
            is_linear = (
                root_weight.shape[2] == 1
                and root_weight.shape[3] == 1
            )
            weight, decompose_mode = extract_conv(
                diff,
                mode,
                linear_mode_param if is_linear else conv_mode_param,
                device = extract_device,
            )
            if decompose_mode == 'low rank':
                extract_a, extract_b, diff = weight
            if small_conv and not is_linear and decompose_mode == 'low rank':
                dim = extract_a.size(0)
                (extract_c, extract_a, _), _ = extract_conv(
                    extract_a.transpose(0, 1),
                    'fixed', dim,
                    extract_device, True
                )
                extract_a = extract_a.transpose(0, 1)
                extract_c = extract_c.transpose(0, 1)
                loras[f'{lora_name}.lora_mid.weight'] = extract_c.detach().cpu().contiguous().half()
                diff = root_weight - torch.einsum(
                    'i j k l, j r, p i -> p r k l',
                    extract_c, extract_a.flatten(1, -1), extract_b.flatten(1, -1)
                ).detach().cpu().contiguous()
                del extract_c

        if decompose_mode == 'low rank':
            loras[f'{lora_name}.lora_down.weight'] = extract_a.detach().cpu().contiguous().half()
            loras[f'{lora_name}.lora_up.weight'] = extract_b.detach().cpu().contiguous().half()
            loras[f'{lora_name}.alpha'] = torch.Tensor([extract_a.shape[0]]).half()
            if use_bias:
                diff = diff.detach().cpu().reshape(extract_b.size(0), -1)
                sparse_diff = make_sparse(diff, sparsity).to_sparse().coalesce()

                indices = sparse_diff.indices().to(torch.int16)
                values = sparse_diff.values().half()
                loras[f'{lora_name}.bias_indices'] = indices
                loras[f'{lora_name}.bias_values'] = values
                loras[f'{lora_name}.bias_size'] = torch.tensor(diff.shape).to(torch.int16)
            del extract_a, extract_b, diff
        elif decompose_mode == 'full':
            loras[f'{lora_name}.diff'] = weight.detach().cpu().contiguous().half()
        else:
            raise NotImplementedError


    def make_state_dict(
        prefix, 
        root_module: torch.nn.Module,
        target_module: torch.nn.Module,
        target_replace_modules,
        target_replace_names = [],
        min_diff = 0.0,
    ):
        loras = {}
        temp = {}
        temp_name = {}
        for name, module in root_module.named_modules():
            if module.__class__.__name__ in target_replace_modules:
                temp[name] = {}
                for child_name, child_module in module.named_modules():
                    if child_module.__class__.__name__ not in {'Linear', 'Conv2d'}:
                        continue
                    temp[name][child_name] = child_module
            elif name in target_replace_names:
                temp_name[name] = module
        for name, module in (pbar:= tqdm(list(target_module.named_modules()), desc=f"Calculating {prefix} svd")):
            pbar.set_description(f"Calculating {prefix} svd: {name}")
            pbar.refresh()
            if name in temp:
                child_modules = temp[name]
                for child_name, child_module in module.named_modules():
                    layer = child_module.__class__.__name__
                    if layer in ['Linear', 'Conv2d']:
                        extract_lora(prefix, child_module, child_modules[child_name], name, child_name, loras, min_diff)
            elif name in temp_name:
                child_module = temp_name[name]
                layer = module.__class__.__name__
                if layer in ['Linear', 'Conv2d']:
                    extract_lora(prefix, module, child_module, name, '', loras, min_diff)

        return loras

    isxl = False
    if len(base_model) == 6: # SDXL case
        isxl = True
        base_model = [base_model[0], base_model[1], base_model[3]]
        db_model = [db_model[0], db_model[1], db_model[3]]
    else:
        base_model = [base_model[0], None, base_model[2]]
        db_model = [db_model[0], None, db_model[2]]

    if isxl:
        prefix_text_encoder = LORA_PREFIX_TEXT_ENCODER1
    else:
        prefix_text_encoder = LORA_PREFIX_TEXT_ENCODER

    text_encoder_loras = make_state_dict(
        prefix_text_encoder,
        base_model[0], db_model[0], 
        TEXT_ENCODER_TARGET_REPLACE_MODULE,
        min_diff = min_diff,
    )
    base_model[0] = None
    db_model[0] = None

    text_encoder_loras2 = {}
    if isxl:
        text_encoder_loras2 = make_state_dict(
            LORA_PREFIX_TEXT_ENCODER2,
            base_model[1], db_model[1],
            TEXT_ENCODER_TARGET_REPLACE_MODULE,
            min_diff = min_diff,
        )
        base_model[1] = None
        db_model[1] = None
    
    unet_loras = make_state_dict(
        LORA_PREFIX_UNET,
        base_model[2], db_model[2], 
        UNET_TARGET_REPLACE_MODULE,
        UNET_TARGET_REPLACE_NAME
    )
    del base_model, db_model
    print(f"Text Encoder: {len(text_encoder_loras) + len(text_encoder_loras2)}, U-Net: {len(unet_loras)}")
    return text_encoder_loras|text_encoder_loras2|unet_loras


def get_module(
    lyco_state_dict: Dict,
    lora_name
):
    if f'{lora_name}.lora_up.weight' in lyco_state_dict:
        up = lyco_state_dict[f'{lora_name}.lora_up.weight']
        down = lyco_state_dict[f'{lora_name}.lora_down.weight']
        mid = lyco_state_dict.get(f'{lora_name}.lora_mid.weight', None)
        alpha = lyco_state_dict.get(f'{lora_name}.alpha', None)
        return 'locon', (up, down, mid, alpha)
    elif f'{lora_name}.hada_w1_a' in lyco_state_dict:
        w1a = lyco_state_dict[f'{lora_name}.hada_w1_a']
        w1b = lyco_state_dict[f'{lora_name}.hada_w1_b']
        w2a = lyco_state_dict[f'{lora_name}.hada_w2_a']
        w2b = lyco_state_dict[f'{lora_name}.hada_w2_b']
        t1 = lyco_state_dict.get(f'{lora_name}.hada_t1', None)
        t2 = lyco_state_dict.get(f'{lora_name}.hada_t2', None)
        alpha = lyco_state_dict.get(f'{lora_name}.alpha', None)
        return 'hada', (w1a, w1b, w2a, w2b, t1, t2, alpha)
    elif f'{lora_name}.weight' in lyco_state_dict:
        weight = lyco_state_dict[f'{lora_name}.weight']
        on_input = lyco_state_dict.get(f'{lora_name}.on_input', False)
        return 'ia3', (weight, on_input)
    elif (f'{lora_name}.lokr_w1' in lyco_state_dict
          or f'{lora_name}.lokr_w1_a' in lyco_state_dict):
        w1 = lyco_state_dict.get(f'{lora_name}.lokr_w1', None)
        w1a = lyco_state_dict.get(f'{lora_name}.lokr_w1_a', None)
        w1b = lyco_state_dict.get(f'{lora_name}.lokr_w1_b', None)
        w2 = lyco_state_dict.get(f'{lora_name}.lokr_w2', None)
        w2a = lyco_state_dict.get(f'{lora_name}.lokr_w2_a', None)
        w2b = lyco_state_dict.get(f'{lora_name}.lokr_w2_b', None)
        t1 = lyco_state_dict.get(f'{lora_name}.lokr_t1', None)
        t2 = lyco_state_dict.get(f'{lora_name}.lokr_t2', None)
        alpha = lyco_state_dict.get(f'{lora_name}.alpha', None)
        return 'kron', (w1, w1a, w1b, w2, w2a, w2b, t1, t2, alpha)
    elif f'{lora_name}.diff' in lyco_state_dict:
        diff = lyco_state_dict[f'{lora_name}.diff']
        diff_b = lyco_state_dict.get(f'{lora_name}.diff_b', None)
        return 'full', (diff, diff_b)
    elif f'{lora_name}.w_norm' in lyco_state_dict:
        w_norm = lyco_state_dict[f'{lora_name}.w_norm']
        b_norm = lyco_state_dict.get(f'{lora_name}.b_norm')
        return 'norm', (w_norm, b_norm)
    else:
        return 'None', ()


def cp_weight_from_conv(
    up, down, mid
):
    up = up.reshape(up.size(0), up.size(1))
    down = down.reshape(down.size(0), down.size(1))
    return torch.einsum('m n w h, i m, n j -> i j w h', mid, up, down)

def cp_weight(
    wa, wb, t
):
    temp = torch.einsum('i j k l, j r -> i r k l', t, wb)
    return torch.einsum('i j k l, i r -> r j k l', temp, wa)


@torch.no_grad()
def rebuild_weight(module_type, params, orig_weight, orig_bias, scale=1):
    if orig_weight is None:
        return orig_weight, orig_bias
    merged = orig_weight
    merged_bias = orig_bias
    if module_type == 'locon':
        up, down, mid, alpha = params
        if alpha is not None:
            scale *= alpha/up.size(1)
        if mid is not None:
            rebuild = cp_weight_from_conv(up, down, mid)
        else:
            rebuild = up.reshape(up.size(0),-1) @ down.reshape(down.size(0), -1)
        merged = orig_weight + rebuild.reshape(orig_weight.shape) * scale
        del up, down, mid, alpha, params, rebuild
    elif module_type == 'hada':
        w1a, w1b, w2a, w2b, t1, t2, alpha = params
        if alpha is not None:
            scale *= alpha / w1b.size(0)
        if t1 is not None:
            rebuild1 = cp_weight(w1a, w1b, t1)
        else:
            rebuild1 = w1a @ w1b
        if t2 is not None:
            rebuild2 = cp_weight(w2a, w2b, t2)
        else:
            rebuild2 = w2a @ w2b
        rebuild = (rebuild1 * rebuild2).reshape(orig_weight.shape)
        merged = orig_weight + rebuild * scale
        del w1a, w1b, w2a, w2b, t1, t2, alpha, params, rebuild, rebuild1, rebuild2
    elif module_type == 'ia3':
        weight, on_input = params
        if not on_input:
            weight = weight.reshape(-1, 1)
        merged = orig_weight + weight * orig_weight * scale
        del weight, on_input, params
    elif module_type == 'kron':
        w1, w1a, w1b, w2, w2a, w2b, t1, t2, alpha = params
        if alpha is not None and (w1b is not None or w2b is not None):
            scale *= alpha / (w1b.size(0) if w1b else w2b.size(0))
        if w1a is not None and w1b is not None:
            if t1:
                w1 = cp_weight(w1a, w1b, t1)
            else:
                w1 = w1a @ w1b
        if w2a is not None and w2b is not None:
            if t2:
                w2 = cp_weight(w2a, w2b, t2)
            else:
                w2 = w2a @ w2b
        if len(w2.shape) == 4:
            w1 = w1.unsqueeze(2).unsqueeze(2)
        w2 = w2.contiguous()
        rebuild = torch.kron(w1, w2).reshape(orig_weight.shape) 
        merged = orig_weight + rebuild* scale
        del w1, w1a, w1b, w2, w2a, w2b, t1, t2, alpha, params, rebuild
    elif module_type == 'full':
        rebuild = params[0].reshape(orig_weight.shape) 
        rebuild_b = params[1].reshape(orig_bias.shape) if orig_bias is not None else None
        merged = orig_weight + rebuild * scale
        if rebuild_b is not None:
            merged_bias = orig_bias + rebuild_b * scale
        del params, rebuild, rebuild_b
    elif module_type == 'norm':
        rebuild = params[0].reshape(orig_weight.shape)
        rebuild_b = params[1].reshape(orig_bias.shape)
        merged = orig_weight + rebuild * scale
        merged_bias = orig_bias + rebuild_b * scale
    else:
        raise NotImplementedError
    
    return merged, merged_bias


@torch.no_grad()
def merge(
    base_model,
    lyco_state_dict,
    scale: float = 1.0,
    device = 'cpu'
):
    UNET_TARGET_REPLACE_MODULE = [
        "Transformer2DModel", 
        "Attention", 
        "ResnetBlock2D", 
        "Downsample2D", 
        "Upsample2D"
    ]
    UNET_TARGET_REPLACE_NAME = [
        "conv_in",
        "conv_out",
        "time_embedding.linear_1",
        "time_embedding.linear_2",
    ]
    TEXT_ENCODER_TARGET_REPLACE_MODULE = ["CLIPAttention", "CLIPMLP"]
    LORA_PREFIX_UNET = 'lora_unet'
    LORA_PREFIX_TEXT_ENCODER = 'lora_te'
    merged = 0
    def merge_state_dict(
        prefix, 
        root_module: torch.nn.Module,
        lyco_state_dict: Dict[str,torch.Tensor],
        target_replace_modules,
        target_replace_names = []
    ):
        nonlocal merged
        for name, module in tqdm(list(root_module.named_modules()), desc=f'Merging {prefix}'):
            if module.__class__.__name__ in target_replace_modules:
                for child_name, child_module in module.named_modules():
                    if child_module.__class__.__name__ not in {'Linear', 'Conv2d'}:
                        continue
                    lora_name = prefix + '.' + name + '.' + child_name
                    lora_name = lora_name.replace('.', '_')
                    
                    result, result_b = rebuild_weight(*get_module(
                        lyco_state_dict, lora_name
                    ), getattr(child_module, 'weight'), getattr(child_module, 'bias', None), scale)
                    if result is not None:
                        merged += 1
                        child_module.requires_grad_(False)
                        child_module.weight.copy_(result)
                    if result_b is not None:
                        child_module.bias.copy_(result_b)
            elif name in target_replace_names:
                lora_name = prefix + '.' + name
                lora_name = lora_name.replace('.', '_')
                    
                result, result_b = rebuild_weight(*get_module(
                    lyco_state_dict, lora_name
                ), getattr(module, 'weight'), getattr(module, 'bias', None), scale)
                if result is not None:
                    merged += 1
                    module.requires_grad_(False)
                    module.weight.copy_(result)
                if result_b is not None:
                    module.bias.copy_(result_b)
    
    for k, v in tqdm(list(lyco_state_dict.items()), desc='Converting Dtype and Device'):
        if device=='cpu':
            lyco_state_dict[k] = v.float().cpu()
        else:
            lyco_state_dict[k] = v.to(device, dtype=base_model[0].parameters().__next__().dtype)
    
    merge_state_dict(
        LORA_PREFIX_TEXT_ENCODER,
        base_model[0].to(device),
        lyco_state_dict,
        TEXT_ENCODER_TARGET_REPLACE_MODULE,
        UNET_TARGET_REPLACE_NAME
    )
    merge_state_dict(
        LORA_PREFIX_UNET,
        base_model[2].to(device),
        lyco_state_dict,
        UNET_TARGET_REPLACE_MODULE,
        UNET_TARGET_REPLACE_NAME
    )
    print(f'{merged} Modules been merged')
