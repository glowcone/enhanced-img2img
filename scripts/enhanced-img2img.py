# Author: OedoSoldier [大江户战士]
# https://space.bilibili.com/55123

import math
import os
import sys
import traceback
import copy
import pandas as pd

import modules.scripts as scripts
import gradio as gr

from modules.processing import Processed, process_images, create_infotext
from PIL import Image, ImageFilter, PngImagePlugin
from modules.shared import opts, cmd_opts, state, total_tqdm
from modules.script_callbacks import ImageSaveParams, before_image_saved_callback
from modules.sd_hijack import model_hijack
if cmd_opts.deepdanbooru:
    import modules.deepbooru as deepbooru

import importlib.util
import re

re_findidx = re.compile(
    r'(?=\S)(\d+)\.(?:[P|p][N|n][G|g]?|[J|j][P|p][G|g]?|[J|j][P|p][E|e][G|g]?|[W|w][E|e][B|b][P|p]?)\b')
re_findname = re.compile(r'[\w-]+?(?=\.)')


def module_from_file(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Script(scripts.Script):
    def title(self):
        return 'Enhanced img2img'

    def description(self):
        return 'Process multiple images with masks'

    def show(self, is_img2img):
        return is_img2img

    def ui(self, is_img2img):
        if not is_img2img:
            return None

        input_dir = gr.Textbox(label='Input directory', lines=1)
        output_dir = gr.Textbox(label='Output directory', lines=1)
        mask_dir = gr.Textbox(label='Mask directory', lines=1)
        with gr.Row():
            use_mask = gr.Checkbox(
                label='Use input image\'s alpha channel as mask')
            use_img_mask = gr.Checkbox(label='Use another image as mask')
            as_output_alpha = gr.Checkbox(
                label='Use mask as output alpha channel')
            is_crop = gr.Checkbox(
                label='Zoom in masked area')

        with gr.Row():
            alpha_threshold = gr.Slider(
                minimum=0,
                maximum=255,
                step=1,
                label='Alpha threshold',
                value=50)

        with gr.Row():
            rotate_img = gr.Radio(
                label='Rotate images (clockwise)', choices=[
                    '0', '-90', '180', '90'], value='0')

        with gr.Row():
            given_file = gr.Checkbox(
                label='Process given file(s) under the input folder, seperate by comma')
            specified_filename = gr.Textbox(label='Files to process', lines=1)

        with gr.Row():
            process_deepbooru = gr.Checkbox(
                label='Use deepbooru prompt',
                visible=cmd_opts.deepdanbooru)
            deepbooru_prev = gr.Checkbox(
                label='Using contextual information',
                visible=cmd_opts.deepdanbooru)

        with gr.Row():
            use_csv = gr.Checkbox(label='Use csv prompt list')
            csv_path = gr.Textbox(label='Input file path', lines=1)

        with gr.Row():
            is_rerun = gr.Checkbox(label='Loopback')

        with gr.Row():
            rerun_width = gr.Slider(
                minimum=64.0,
                maximum=2048.0,
                step=64.0,
                label='Firstpass width',
                value=512.0)
            rerun_height = gr.Slider(
                minimum=64.0,
                maximum=2048.0,
                step=64.0,
                label='Firstpass height',
                value=512.0)
            rerun_strength = gr.Slider(
                minimum=0.0,
                maximum=1.0,
                step=0.01,
                label='Denoising strength',
                value=0.2)

        with gr.Accordion('Depthmap settings', open=False):
            '''
            Borrowed from https://github.com/Extraltodeus/depthmap2mask
            Author: Extraltodeus
            '''
            use_depthmap = gr.Checkbox(
                label='Use depthmap model to create mask')
            treshold = gr.Slider(
                minimum=0,
                maximum=255,
                step=1,
                label='Contrasts cut level',
                value=0)
            match_size = gr.Checkbox(label='Match input size', value=True)
            net_width = gr.Slider(
                minimum=64,
                maximum=2048,
                step=64,
                label='Net width',
                value=384)
            net_height = gr.Slider(
                minimum=64,
                maximum=2048,
                step=64,
                label='Net height',
                value=384)
            with gr.Row():
                invert_depth = gr.Checkbox(
                    label='Invert DepthMap', value=False)
                override_mask_blur = gr.Checkbox(
                    label='Override mask blur to 0', value=True)
                override_fill = gr.Checkbox(
                    label='Override inpaint to original', value=True)
                clean_cut = gr.Checkbox(
                    label='Turn the depthmap into absolute black/white', value=False)
            model_type = gr.Dropdown(
                label='Model',
                choices=[
                    'dpt_large',
                    'midas_v21',
                    'midas_v21_small'],
                value='midas_v21_small',
                type='index',
                elem_id='model_type')

        return [
            input_dir,
            output_dir,
            mask_dir,
            use_mask,
            use_img_mask,
            as_output_alpha,
            is_crop,
            alpha_threshold,
            rotate_img,
            given_file,
            specified_filename,
            process_deepbooru,
            deepbooru_prev,
            use_csv,
            csv_path,
            is_rerun,
            rerun_width,
            rerun_height,
            rerun_strength,
            use_depthmap,
            treshold,
            match_size,
            net_width,
            net_height,
            invert_depth,
            model_type,
            override_mask_blur,
            override_fill,
            clean_cut]

    def run(
            self,
            p,
            input_dir,
            output_dir,
            mask_dir,
            use_mask,
            use_img_mask,
            as_output_alpha,
            is_crop,
            alpha_threshold,
            rotate_img,
            given_file,
            specified_filename,
            process_deepbooru,
            deepbooru_prev,
            use_csv,
            csv_path,
            is_rerun,
            rerun_width,
            rerun_height,
            rerun_strength,
            use_depthmap,
            treshold,
            match_size,
            net_width,
            net_height,
            invert_depth,
            model_type,
            override_mask_blur,
            override_fill,
            clean_cut):

        def remap_range(value, minIn, MaxIn, minOut, maxOut):
            if value > MaxIn:
                value = MaxIn
            if value < minIn:
                value = minIn
            finalValue = ((value - minIn) / (MaxIn - minIn)) * \
                (maxOut - minOut) + minOut
            return finalValue

        def create_depth_mask_from_depth_map(
                img, p, treshold, clean_cut):
            img = copy.deepcopy(img.convert('RGBA'))
            mask_img = copy.deepcopy(img.convert('L'))
            mask_datas = mask_img.getdata()
            datas = img.getdata()
            newData = []
            maxD = max(mask_datas)
            if clean_cut and treshold == 0:
                treshold = 128
            for i in range(len(mask_datas)):
                if clean_cut and mask_datas[i] > treshold:
                    newrgb = 255
                elif mask_datas[i] > treshold and not clean_cut:
                    newrgb = int(
                        remap_range(
                            mask_datas[i],
                            treshold,
                            255,
                            0,
                            255))
                else:
                    newrgb = 0
                newData.append((newrgb, newrgb, newrgb, 255))
            img.putdata(newData)
            return img

        util = module_from_file(
            'util', 'extensions/enhanced-img2img/scripts/util.py').CropUtils()
        if use_depthmap:
            sdmg = module_from_file(
                'depthmap',
                'extensions/enhanced-img2img/scripts/depthmap_for_depth2img.py')

            img_x = p.width if match_size else net_width
            img_y = p.height if match_size else net_height

            sdmg = sdmg.SimpleDepthMapGenerator(
                model_type, img_x, img_y, invert_depth)

        rotation_dict = {
            '-90': Image.Transpose.ROTATE_90,
            '180': Image.Transpose.ROTATE_180,
            '90': Image.Transpose.ROTATE_270}

        if use_mask:
            mask_dir = input_dir
            use_img_mask = True
            as_output_alpha = True

        if is_rerun:
            original_strength = copy.deepcopy(p.denoising_strength)
            original_size = (copy.deepcopy(p.width), copy.deepcopy(p.height))

        if process_deepbooru:
            deepbooru.model.start()

        if use_csv:
            prompt_list = [
                i[0] for i in pd.read_csv(
                    csv_path,
                    header=None).values.tolist()]
        init_prompt = p.prompt

        initial_info = None
        if given_file:
            images = []
            images_in_folder = [
                file for file in [
                    os.path.join(
                        input_dir,
                        x) for x in os.listdir(input_dir)] if os.path.isfile(file)]
            try:
                images_idx = [int(re.findall(re_findidx, j)[0])
                              for j in images_in_folder]
            except BaseException:
                images_idx = [re.findall(re_findname, j)[0]
                              for j in images_in_folder]
            images_in_folder_dict = dict(zip(images_idx, images_in_folder))
            sep = ',' if ',' in specified_filename else ' '
            for i in specified_filename.split(','):
                if i in images_in_folder:
                    images.append(i)
                else:
                    try:
                        match = re.search(r'(^\d*)-(\d*$)', i)
                        if match:
                            start, end = match.groups()
                            if start == '':
                                start = images_idx[0]
                            if end == '':
                                end = images_idx[-1]
                            images += [images_in_folder_dict[j]
                                       for j in list(range(int(start), int(end) + 1))]
                    except BaseException:
                        images.append(images_in_folder_dict[int(i)])
            if len(images) == 0:
                raise FileNotFoundError

        else:
            images = [
                file for file in [
                    os.path.join(
                        input_dir,
                        x) for x in os.listdir(input_dir)] if os.path.isfile(file)]

        print(f'Will process following files: {", ".join(images)}')

        if use_img_mask:
            try:
                masks = [
                    re.findall(
                        re_findidx,
                        file)[0] for file in [
                        os.path.join(
                            mask_dir,
                            x) for x in os.listdir(mask_dir)] if os.path.isfile(file)]
            except BaseException:
                masks = [
                    re.findall(
                        re_findname,
                        file)[0] for file in [
                        os.path.join(
                            mask_dir,
                            x) for x in os.listdir(mask_dir)] if os.path.isfile(file)]

            masks_in_folder = [
                file for file in [
                    os.path.join(
                        mask_dir,
                        x) for x in os.listdir(mask_dir)] if os.path.isfile(file)]

            masks_in_folder_dict = dict(zip(masks, masks_in_folder))

        else:
            masks = images

        p.img_len = 1
        p.do_not_save_grid = True
        p.do_not_save_samples = True

        state.job_count = 1

        if process_deepbooru and deepbooru_prev:
            prev_prompt = ['']

        frame = 0

        img_len = len(images)
        if is_rerun:
            state.job_count *= 2 * len(images)
        else:
            state.job_count *= len(images)

        for idx, path in enumerate(images):
            if state.interrupted:
                break
            batch_images = []
            batched_raw = []
            cropped, mask, crop_info = None, None, None
            print(f'Processing: {path}')
            try:
                img = Image.open(path)
                if rotate_img != '0':
                    img = img.transpose(rotation_dict[rotate_img])
                if use_img_mask and not use_depthmap:
                    try:
                        to_process = re.findall(re_findidx, path)[0]
                    except BaseException:
                        to_process = re.findall(re_findname, path)[0]
                    try:
                        mask = Image.open(masks_in_folder_dict[to_process])
                        a = mask.split()[-1].convert('L').point(
                            lambda x: 255 if x > alpha_threshold else 0)
                        mask = Image.merge('RGBA', (a, a, a, a.convert('L')))
                    except BaseException:
                        print(
                            f'Mask of {os.path.basename(path)} is not found, output original image!')
                        img.save(
                            os.path.join(
                                output_dir,
                                os.path.basename(path)))
                        continue
                    if rotate_img != '0':
                        mask = mask.transpose(
                            rotation_dict[rotate_img])
                    if is_crop:
                        cropped, mask, crop_info = util.crop_img(
                            img.copy(), mask, alpha_threshold)
                        if not mask:
                            print(
                                f'Mask of {os.path.basename(path)} is blank, output original image!')
                            img.save(
                                os.path.join(
                                    output_dir,
                                    os.path.basename(path)))
                            continue
                        batched_raw.append(img.copy())
                elif use_depthmap:
                    mask = sdmg.calculate_depth_maps(img)

                    if treshold > 0 or clean_cut:
                        mask = create_depth_mask_from_depth_map(
                            mask, p, treshold, clean_cut)

                    if is_crop:
                        cropped, mask, crop_info = util.crop_img(
                            img.copy(), mask, treshold)
                        if not mask:
                            print(
                                f'Mask of {os.path.basename(path)} is blank, output original image!')
                            img.save(
                                os.path.join(
                                    output_dir,
                                    os.path.basename(path)))
                            continue
                        batched_raw.append(img.copy())
                img = cropped if cropped is not None else img
                batch_images.append((img, path))

            except BaseException:
                print(f'Error processing {path}:', file=sys.stderr)
                print(traceback.format_exc(), file=sys.stderr)

            if len(batch_images) == 0:
                print('No images will be processed.')
                break

            if process_deepbooru:
                deepbooru_prompt = deepbooru.model.tag_multi(
                    batch_images[0][0])
                if deepbooru_prev:
                    deepbooru_prompt = deepbooru_prompt.split(', ')
                    common_prompt = list(
                        set(prev_prompt) & set(deepbooru_prompt))
                    p.prompt = init_prompt + ', '.join(common_prompt) + ', '.join(
                        [i for i in deepbooru_prompt if i not in common_prompt])
                    prev_prompt = deepbooru_prompt
                else:
                    if len(init_prompt) > 0:
                        init_prompt += ', '
                    p.prompt = init_prompt + deepbooru_prompt

            if use_csv:
                if len(init_prompt) > 0:
                    init_prompt += ', '
                p.prompt = init_prompt + prompt_list[frame]

            state.job = f'{idx} out of {img_len}: {batch_images[0][1]}'
            p.init_images = [x[0] for x in batch_images]

            if override_mask_blur and use_depthmap:
                p.mask_blur = 0
            if override_fill and use_depthmap:
                p.inpainting_fill = 1

            if mask is not None and (use_mask or use_img_mask or use_depthmap):
                p.image_mask = mask

            def process_images_with_size(p, size, strength):
                p.width, p.height, = size
                p.strength = strength
                return process_images(p)

            if is_rerun:
                proc = process_images_with_size(p, (rerun_width, rerun_height), rerun_strength)
                p_2 = p
                p_2.init_images = proc.images
                proc = process_images_with_size(p_2, original_size, original_strength)
            else:
                proc = process_images(p)

            if initial_info is None:
                initial_info = proc.info
            for output, (input_img, path) in zip(proc.images, batch_images):
                filename = os.path.basename(path)
                if use_img_mask:
                    if as_output_alpha:
                        output.putalpha(
                            p.image_mask.resize(
                                output.size).convert('L'))

                if rotate_img != '0':
                    output = output.transpose(
                        rotation_dict[str(-int(rotate_img))])

                if is_crop:
                    output = util.restore_by_file(
                        batched_raw[0],
                        output,
                        batch_images[0][0],
                        mask,
                        crop_info,
                        p.mask_blur + 1)

                comments = {}
                if len(model_hijack.comments) > 0:
                    for comment in model_hijack.comments:
                        comments[comment] = 1

                info = create_infotext(
                    p,
                    p.all_prompts,
                    p.all_seeds,
                    p.all_subseeds,
                    comments,
                    0,
                    0)
                pnginfo = {}
                if info is not None:
                    pnginfo['parameters'] = info

                params = ImageSaveParams(output, p, filename, pnginfo)
                before_image_saved_callback(params)
                fullfn_without_extension, extension = os.path.splitext(
                    filename)

                if is_rerun:
                    params.pnginfo['loopback_params'] = f'Firstpass size: {rerun_width}x{rerun_height}, Firstpass strength: {original_strength}'

                info = params.pnginfo.get('parameters', None)

                def exif_bytes():
                    return piexif.dump({
                        'Exif': {
                            piexif.ExifIFD.UserComment: piexif.helper.UserComment.dump(info or '', encoding='unicode')
                        },
                    })

                if extension.lower() == '.png':
                    pnginfo_data = PngImagePlugin.PngInfo()
                    for k, v in params.pnginfo.items():
                        pnginfo_data.add_text(k, str(v))

                    output.save(
                        os.path.join(
                            output_dir,
                            filename),
                        pnginfo=pnginfo_data)

                elif extension.lower() in ('.jpg', '.jpeg', '.webp'):
                    output.save(os.path.join(output_dir, filename))

                    if opts.enable_pnginfo and info is not None:
                        piexif.insert(
                            exif_bytes(), os.path.join(
                                output_dir, filename))
                else:
                    output.save(os.path.join(output_dir, filename))

            frame += 1

        if process_deepbooru:
            deepbooru.model.stop()

        return Processed(p, [], p.seed, initial_info)
