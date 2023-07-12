import json
import os
import random
import torch
import os, binascii

from lib.augment import AugmentTransforms
from pathlib import Path
from PIL import Image
from torchvision import transforms
from tqdm.auto import tqdm
from lib.utils import get_local_rank
from data.buckets import AspectRatioBucket


class ImageStore(torch.utils.data.IterableDataset):
    """
    A dataset to prepare the instance and class images with the prompts for fine-tuning the model.
    It pre-processes the images and the tokenizes prompts.
    """

    def __init__(
        self,
        img_path,
        size=512,
        center_crop=False,
        max_length=225,
        ucg=0,
        rank=0,
        augment=None,
        process_tags=True,
        tokenizer=None,
        important_tags=[],
        allow_duplicates=False,
        **kwargs
    ):
        self.size = size
        self.fliter_tags = process_tags
        self.center_crop = center_crop
        self.ucg = ucg
        self.max_length = max_length
        self.tokenizer=tokenizer
        self.rank = rank
        self.dataset = img_path
        self.use_latent_cache = False
        self.allow_duplicates = allow_duplicates
        self.important_tags = important_tags
        self.image_transforms = transforms.Compose(
            [
                transforms.Resize(size, interpolation=transforms.InterpolationMode.LANCZOS),
                transforms.CenterCrop(size) if center_crop else transforms.RandomCrop(size),
                transforms.ToTensor(),
                transforms.Normalize([0.5], [0.5]),
            ]
        )
        
        self.yandere_tags = {}
        self.latent_cache = {}
        self.augment = AugmentTransforms(augment)
        print()
        
        # https://huggingface.co/datasets/nyanko7/yandere-images/blob/main/yandere-tags.json
        if Path("yandere-tags.json").is_file():
            with open("yandere-tags.json") as f:
                self.yandere_tags = json.loads(f.read())
            print(f"Read {len(self.yandere_tags)} tags from yandere-tags.json")
            
        self.update_store()

    def prompt_resolver(self, x: str):
        img_item = (x, "")
        fp = os.path.splitext(x)[0]

        with open(fp + ".txt") as f:
            content = f.read()
            new_prompt = content

        return str(x), new_prompt

    def update_store(self):   
        self.entries = []
        bar = tqdm(total=-1, desc=f"Loading captions", disable=self.rank not in [0, -1])
        folders = []
        for entry in self.dataset:
            if self.allow_duplicates and not isinstance(entry, str):
                folders.extend([entry[0] for _ in range(entry[1])])
            else:
                folders.append(entry)
        
        for entry in folders:            
            for x in Path(entry).rglob("*"):
                if not (x.is_file() and x.suffix in [".jpg", ".png", ".webp", ".bmp", ".gif", ".jpeg", ".tiff"]):
                    continue
                img, prompt = self.prompt_resolver(x)
                _, skip = self.process_tags(prompt)
                if skip:
                    continue
                
                if self.allow_duplicates:
                    prefix = binascii.hexlify(os.urandom(5))
                    img = f"{prefix}@{img}"
                
                self.entries.append((img, prompt))
                bar.update(1)

        self._length = len(self.entries)
        random.shuffle(self.entries)
        
    def cache_latents(self, vae, store=None, config=None):
        self.use_latent_cache = True
        self.latents_cache = {}
        for entry in tqdm(self.entries, desc=f"Caching latents", disable=get_local_rank() not in [0, -1]):
            image = torch.stack([self.image_transforms(self.read_img(entry[0]))])
            latent = vae.encode(image.to(vae.device, dtype=vae.dtype)).latent_dist.sample() * 0.18215
            self.latents_cache[entry[0]] = latent.detach().squeeze(0).cpu()

    def tokenize(self, prompt):
        '''
        Handle token truncation in collate_fn()
        '''
        return self.tokenizer(
            prompt,
            padding="do_not_pad",
            truncation=True,
            max_length=self.max_length,
        ).input_ids 

    def read_img(self, filepath):  
        if self.allow_duplicates and "@" in filepath:
            filepath = filepath[filepath.index("@")+1:]    
        img = Image.open(filepath)
        if not img.mode == "RGB":
            img = img.convert("RGB")
        return img

    def process_tags(self, tags, min_tags=24, max_tags=72, type_dropout=0.75, keep_important=1.00, keep_jpeg_artifacts=True, sort_tags=False):
        if not self.fliter_tags:
            return tags, False
        
        if isinstance(tags, str):
            tags = tags.replace(",", " ").split(" ")
            tags = [tag.strip() for tag in tags if tag != ""]
        final_tags = {}

        tag_dict = {tag: True for tag in tags}
        pure_tag_dict = {tag.split(":", 1)[-1]: tag for tag in tags}
        for bad_tag in ["absurdres", "highres", "translation_request", "translated", "commentary", "commentary_request", "commentary_typo", "character_request", "bad_id", "bad_link", "bad_pixiv_id", "bad_twitter_id", "bad_tumblr_id", "bad_deviantart_id", "bad_nicoseiga_id", "md5_mismatch", "cosplay_request", "artist_request", "wide_image", "author_request", "artist_name"]:
            if bad_tag in pure_tag_dict:
                del tag_dict[pure_tag_dict[bad_tag]]

        if "rating:questionable" in tag_dict or "rating:explicit" in tag_dict or "nsfw" in tag_dict:
            final_tags["nsfw"] = True

        base_chosen = []
        skip_image = False
        # counts = [0]
        
        for tag in tag_dict.keys():
            # For yande.re tags.
            if len(self.yandere_tags) <= 0 or tag not in self.yandere_tags:
                continue
            
            if int(self.yandere_tags[tag]["type"]) in [1, 3, 4, 5] and random.random() < keep_important:
                base_chosen.append(tag)
                      
        for tag in tag_dict.keys():
            # For danbooru tags.
            parts = tag.split(":", 1)
            if parts[0] in self.important_tags and random.random() < keep_important:
                base_chosen.append(tag)
            if parts[0] in ["artist", "copyright", "character"] and random.random() < keep_important:
                base_chosen.append(tag)
            if len(parts[-1]) > 1 and parts[-1][0] in ["1", "2", "3", "4", "5", "6"] and parts[-1][1:] in ["boy", "boys", "girl", "girls"]:
                base_chosen.append(tag)
            if parts[-1] in ["6+girls", "6+boys", "bad_anatomy", "bad_hands"]:
                base_chosen.append(tag)

        tag_count = min(random.randint(min_tags, max_tags), len(tag_dict.keys()))
        base_chosen_set = set(base_chosen)
        chosen_tags = base_chosen + [tag for tag in random.sample(list(tag_dict.keys()), tag_count) if tag not in base_chosen_set]
        if sort_tags:
            chosen_tags = sorted(chosen_tags)

        for tag in chosen_tags:
            tag = tag.replace(",", "").replace("_", " ")
            if random.random() < type_dropout:
                if tag.startswith("artist:"):
                    tag = tag[7:]
                elif tag.startswith("copyright:"):
                    tag = tag[10:]
                elif tag.startswith("character:"):
                    tag = tag[10:]
                elif tag.startswith("general:"):
                    tag = tag[8:]
            if tag.startswith("meta:"):
                tag = tag[5:]
            final_tags[tag] = True

        for bad_tag in ["comic", "panels", "everyone", "sample_watermark", "text_focus", "text", "tagme"]:
            if bad_tag in pure_tag_dict:
                skip_image = True
        if not keep_jpeg_artifacts and "jpeg_artifacts" in tag_dict:
            skip_image = True
            
        return "Tags: " + ", ".join(list(final_tags.keys())), skip_image
    
    def collate_fn(self, examples):
        input_ids = [example["prompt_ids"] for example in examples]
        pixel_values = [example["images"] for example in examples]

        pixel_values = torch.stack(pixel_values).to(memory_format=torch.contiguous_format).float()
        input_ids = self.tokenizer.pad({"input_ids": input_ids}, padding=True, return_tensors="pt").input_ids
        
        return [input_ids, pixel_values]

    def __len__(self):
        return self._length

    def __iter__(self):
        for entry in self.entries:
            example = {}
            instance_path, instance_prompt = entry
            
            if self.use_latent_cache:
                example["images"] = self.latents_cache[instance_path]
            else:
                instance_image = self.read_img(instance_path)
                example["images"] = self.image_transforms(instance_image)
                
            example["prompt_ids"] = self.tokenize(instance_prompt)
            yield example


class AspectRatioDataset(ImageStore):
    def __init__(self, arb_config, debug_arb=False, **kwargs):
        super().__init__(**kwargs)
        self.debug = debug_arb
        self.prompt_cache = {}
        self.kwargs = kwargs

        self.arb_config = arb_config
        if arb_config["debug"]:
            print(f"BucketManager initialized using config: {self.arb_config}")
        else:
            print(f"BucketManager initialized with base_res = {self.arb_config['base_res']}, max_size = {self.arb_config['max_size']}")
        
        for path, prompt in self.entries:
            self.prompt_cache[path] = prompt
        
        self.init_buckets()

    def init_buckets(self):
        entries = [x[0] for x in self.entries]
        self.buckets = AspectRatioBucket(self.get_dict(entries), **self.arb_config)
        
    def get_dict(self, entries):
        id_size_map = {}
        for entry in tqdm(iter(entries), desc=f"Loading resolutions", disable=self.rank not in [0, -1]):
            fp = entry[entry.index("@")+1:] if self.kwargs.get("allow_duplicates") else entry
            with Image.open(fp) as img:
                size = img.size
            id_size_map[entry] = size
        return id_size_map
            
    def cache_latents(self, vae, store, config):
        self.use_latent_cache = True
        self.latents_cache = {}
        progress_bar = tqdm(total=len(self.entries), desc=f"Caching latents", disable=get_local_rank() not in [0, -1])
        for entry in store.buckets.keys():
            size = store.resolutions[entry]
            imgs = store.buckets[entry][:]
            for img in imgs:
                img_data = self.read_img(img)
                img_data_actual = torch.stack([self.transformer(img_data, size, config.dataset.center_crop)]).float()
                img_data_base = torch.stack([self.transformer(img_data, config.arb.base_res, True)]).float()
                latent = vae.encode(img_data_actual.to(vae.device, dtype=vae.dtype)).latent_dist.sample() * 0.18215
                latent_base = vae.encode(img_data_base.to(vae.device, dtype=vae.dtype)).latent_dist.sample() * 0.18215
                self.latents_cache[str(img)] = {
                    "latent": latent.detach().squeeze(0).cpu(),
                    "latent_base": latent_base.detach().squeeze(0).cpu(),
                    "size": size
                }
                progress_bar.update()
        progress_bar.close()
        
    def denormalize(self, img, mean=0.5, std=0.5):
        res = transforms.Normalize((-1*mean/std), (1.0/std))(img.squeeze(0))
        res = torch.clamp(res, 0, 1)
        return res

    def transformer(self, img, size, center_crop=False):
        x, y = img.size
        short, long = (x, y) if x <= y else (y, x)

        w, h = size
        min_crop, max_crop = (w, h) if w <= h else (h, w)
        ratio_src, ratio_dst = float(long / short), float(max_crop / min_crop)

        if ratio_src > ratio_dst:
            new_w, new_h = (min_crop, int(min_crop * ratio_src)) if x < y else (int(min_crop * ratio_src), min_crop)
        elif ratio_src < ratio_dst:
            new_w, new_h = (max_crop, int(max_crop / ratio_src)) if x > y else (int(max_crop / ratio_src), max_crop)
        else:
            new_w, new_h = w, h

        image_transforms = transforms.Compose([
            transforms.Resize((new_h, new_w), interpolation=transforms.InterpolationMode.LANCZOS),
            transforms.CenterCrop((h, w)) if center_crop else transforms.RandomCrop((h, w)),
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5])
        ])

        new_img = image_transforms(img)
        return new_img

    def build_dict(self, item, roll) -> dict:
        item_id, size, = item["instance"], item["size"],

        if item_id == "":
            return {}

        prompt, _ = self.process_tags(self.prompt_cache[item_id])
        if random.random() < self.ucg:
            prompt = ''
        
        if not self.use_latent_cache:
            image = self.augment.transform(self.read_img(item_id), roll)
            image = self.transformer(image, size)
        else:
            entry = self.latents_cache[str(item_id)]
            image = entry["latent"] if (entry["size"] == size).all() else entry["latent_base"]

        example = {
            f"images": image,
            f"prompt_ids": self.tokenize(prompt)
        }
        return example

    def __iter__(self):
        for batch, size in self.buckets.generator():
            for item in batch:
                rs = random.random()
                yield self.build_dict({"size": size, "instance": item}, rs)

