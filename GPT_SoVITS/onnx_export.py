import librosa
import os
from module.models_onnx import SynthesizerTrn, symbols
from AR.models.t2s_lightning_module_onnx import Text2SemanticLightningModule
import torch
import torchaudio
from torch import nn
from feature_extractor import cnhubert

cnhubert_base_path = "GPT_SoVITS/pretrained_models/chinese-hubert-base"
cnhubert.cnhubert_base_path=cnhubert_base_path
ssl_model = cnhubert.get_model()
from text import cleaned_text_to_sequence
import soundfile
from tools.my_utils import load_audio
import json
import text.cleaner

def spectrogram_torch(y, n_fft, sampling_rate, hop_size, win_size, center=False):
    hann_window = torch.hann_window(win_size).to(
            dtype=y.dtype, device=y.device
        )
    y = torch.nn.functional.pad(
        y.unsqueeze(1),
        (int((n_fft - hop_size) / 2), int((n_fft - hop_size) / 2)),
        mode="reflect",
    )
    y = y.squeeze(1)
    spec = torch.stft(
        y,
        n_fft,
        hop_length=hop_size,
        win_length=win_size,
        window=hann_window,
        center=center,
        pad_mode="reflect",
        normalized=False,
        onesided=True,
        return_complex=False,
    )
    spec = torch.sqrt(spec.pow(2).sum(-1) + 1e-6)
    return spec


class DictToAttrRecursive(dict):
    def __init__(self, input_dict):
        super().__init__(input_dict)
        for key, value in input_dict.items():
            if isinstance(value, dict):
                value = DictToAttrRecursive(value)
            self[key] = value
            setattr(self, key, value)

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(f"Attribute {item} not found")

    def __setattr__(self, key, value):
        if isinstance(value, dict):
            value = DictToAttrRecursive(value)
        super(DictToAttrRecursive, self).__setitem__(key, value)
        super().__setattr__(key, value)

    def __delattr__(self, item):
        try:
            del self[item]
        except KeyError:
            raise AttributeError(f"Attribute {item} not found")


class T2SEncoder(nn.Module):
    def __init__(self, t2s, vits):
        super().__init__()
        self.encoder = t2s.onnx_encoder
        self.vits = vits
    
    def forward(self, ref_seq, text_seq, ref_bert, text_bert, ssl_content):
        codes = self.vits.extract_latent(ssl_content)
        prompt_semantic = codes[0, 0]
        bert = torch.cat([ref_bert.transpose(0, 1), text_bert.transpose(0, 1)], 1)
        all_phoneme_ids = torch.cat([ref_seq, text_seq], 1)
        bert = bert.unsqueeze(0)
        prompt = prompt_semantic.unsqueeze(0)
        return self.encoder(all_phoneme_ids, bert), prompt


class T2SModel(nn.Module):
    def __init__(self, t2s_path, vits_model):
        super().__init__()
        dict_s1 = torch.load(t2s_path, map_location="cpu")
        self.config = dict_s1["config"]
        self.t2s_model = Text2SemanticLightningModule(self.config, "ojbk", is_train=False)
        self.t2s_model.load_state_dict(dict_s1["weight"])
        self.t2s_model.eval()
        self.vits_model = vits_model.vq_model
        self.hz = 50
        self.max_sec = self.config["data"]["max_sec"]
        self.t2s_model.model.top_k = torch.LongTensor([self.config["inference"]["top_k"]])
        self.t2s_model.model.early_stop_num = torch.LongTensor([self.hz * self.max_sec])
        self.t2s_model = self.t2s_model.model
        self.t2s_model.init_onnx()
        self.onnx_encoder = T2SEncoder(self.t2s_model, self.vits_model)
        self.first_stage_decoder = self.t2s_model.first_stage_decoder
        self.stage_decoder = self.t2s_model.stage_decoder
        #self.t2s_model = torch.jit.script(self.t2s_model)

    def forward(self, ref_seq, text_seq, ref_bert, text_bert, ssl_content):
        early_stop_num = self.t2s_model.early_stop_num

        #[1,N] [1,N] [N, 1024] [N, 1024] [1, 768, N]
        x, prompts = self.onnx_encoder(ref_seq, text_seq, ref_bert, text_bert, ssl_content)

        prefix_len = prompts.shape[1]

        #[1,N,512] [1,N]
        y, k, v, y_emb, x_example = self.first_stage_decoder(x, prompts)

        stop = False
        for idx in range(1, 1500):
            #[1, N] [N_layer, N, 1, 512] [N_layer, N, 1, 512] [1, N, 512] [1] [1, N, 512] [1, N]
            enco = self.stage_decoder(y, k, v, y_emb, x_example)
            y, k, v, y_emb, logits, samples = enco
            if early_stop_num != -1 and (y.shape[1] - prefix_len) > early_stop_num:
                stop = True
            argmax = torch.argmax(logits, dim=-1)[0]
            first_sample = samples[0, 0]
            # print("argmax is: ", argmax)
            if argmax == self.t2s_model.EOS or first_sample == self.t2s_model.EOS:
                stop = True
            if stop:
                break
        y[0, -1] = 0

        return y[:, -idx:].unsqueeze(0)

    def export(self, ref_seq, text_seq, ref_bert, text_bert, ssl_content, project_name, dynamo=False):
        #self.onnx_encoder = torch.jit.script(self.onnx_encoder)
        if dynamo:
            export_options = torch.onnx.ExportOptions(dynamic_shapes=True)
            onnx_encoder_export_output = torch.onnx.dynamo_export(
                self.onnx_encoder,
                (ref_seq, text_seq, ref_bert, text_bert, ssl_content),
                export_options=export_options
            )
            onnx_encoder_export_output.save(f"onnx/{project_name}/{project_name}_t2s_encoder.onnx")
            return

        torch.onnx.export(
            self.onnx_encoder,
            (ref_seq, text_seq, ref_bert, text_bert, ssl_content),
            f"onnx/{project_name}/{project_name}_t2s_encoder.onnx",
            input_names=["ref_seq", "text_seq", "ref_bert", "text_bert", "ssl_content"],
            output_names=["x", "prompts"],
            dynamic_axes={
                "ref_seq": {1: "ref_length"},
                "text_seq": {1: "text_length"},
                "ref_bert": {0: "ref_length"},
                "text_bert": {0: "text_length"},
                "ssl_content": {2: "ssl_length"},
            },
            opset_version=17
        )
        x, prompts = self.onnx_encoder(ref_seq, text_seq, ref_bert, text_bert, ssl_content)

        top_k = self.t2s_model.top_k
        top_p = 1
        temperature = 1
        repetition_penalty = 1.35

        torch.onnx.export(
            self.first_stage_decoder,
            (x, prompts),
            f"onnx/{project_name}/{project_name}_t2s_fsdec.onnx",
            input_names=["x", "prompts", "top_k", "top_p", "temperature", "repetition_penalty"],
            output_names=["y", "k", "v", "y_emb", "x_example"],
            dynamic_axes={
                "x": {1: "x_length"},
                "prompts": {1: "prompts_length"},
            },
            verbose=False,
            opset_version=17
        )
        y, k, v, y_emb, x_example = self.first_stage_decoder(x, prompts)

        torch.onnx.export(
            self.stage_decoder,
            (y, k, v, y_emb, x_example),
            f"onnx/{project_name}/{project_name}_t2s_sdec.onnx",
            input_names=["iy", "ik", "iv", "iy_emb", "ix_example", "top_k", "top_p", "temperature", "repetition_penalty"],
            output_names=["y", "k", "v", "y_emb", "logits", "samples"],
            dynamic_axes={
                "iy": {1: "iy_length"},
                "ik": {1: "ik_length"},
                "iv": {1: "iv_length"},
                "iy_emb": {1: "iy_emb_length"},
                "ix_example": {1: "ix_example_length"},
            },
            verbose=False,
            opset_version=17
        )


class VitsModel(nn.Module):
    def __init__(self, vits_path):
        super().__init__()
        dict_s2 = torch.load(vits_path,map_location="cpu")
        self.hps = dict_s2["config"]
        self.hps = DictToAttrRecursive(self.hps)
        self.hps.model.semantic_frame_rate = "25hz"
        if dict_s2['weight']['enc_p.text_embedding.weight'].shape[0] == 322:
            self.hps.model.version = "v1"
        else:
            self.hps.model.version = "v2"
        self.vq_model = SynthesizerTrn(
            self.hps.data.filter_length // 2 + 1,
            self.hps.train.segment_size // self.hps.data.hop_length,
            n_speakers=self.hps.data.n_speakers,
            # version=self.hps.model.version,
            **self.hps.model
        )
        self.vq_model.eval()
        print(self.vq_model.load_state_dict(dict_s2["weight"], strict=False))
        
    def forward(self, text_seq, pred_semantic, ref_audio):
        refer = spectrogram_torch(
            ref_audio,
            self.hps.data.filter_length,
            self.hps.data.sampling_rate,
            self.hps.data.hop_length,
            self.hps.data.win_length,
            center=False
        )
        return self.vq_model(pred_semantic, text_seq, refer)[0, 0]


class GptSoVits(nn.Module):
    def __init__(self, vits, t2s):
        super().__init__()
        self.vits = vits
        self.t2s = t2s
    
    def forward(self, ref_seq, text_seq, ref_bert, text_bert, ref_audio, ssl_content, debug=False):
        pred_semantic = self.t2s(ref_seq, text_seq, ref_bert, text_bert, ssl_content)
        audio = self.vits(text_seq, pred_semantic, ref_audio)
        if debug:
            import onnxruntime
            sess = onnxruntime.InferenceSession("onnx/nahida/nahida_vits.onnx", providers=["CPUExecutionProvider"])
            audio1 = sess.run(None, {
                "text_seq" : text_seq.detach().cpu().numpy(),
                "pred_semantic" : pred_semantic.detach().cpu().numpy(), 
                "ref_audio" : ref_audio.detach().cpu().numpy()
            })
            return audio, audio1
        return audio

    def export(self, ref_seq, text_seq, ref_bert, text_bert, ref_audio, ssl_content, project_name):
        self.t2s.export(ref_seq, text_seq, ref_bert, text_bert, ssl_content, project_name)
        pred_semantic = self.t2s(ref_seq, text_seq, ref_bert, text_bert, ssl_content)
        torch.onnx.export(
            self.vits,
            (text_seq, pred_semantic, ref_audio),
            f"onnx/{project_name}/{project_name}_vits.onnx",
            input_names=["text_seq", "pred_semantic", "ref_audio"],
            output_names=["audio"],
            dynamic_axes={
                "text_seq": {1 : "text_length"},
                "pred_semantic": {2 : "pred_length"},
                "ref_audio": {1 : "audio_length"},
            },
            opset_version=17,
            verbose=False
        )


class SSLModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.ssl = ssl_model

    def forward(self, ref_audio_16k):
        return self.ssl.model(ref_audio_16k)["last_hidden_state"].transpose(1, 2)

    def export(self, ref_audio_16k, project_name):
        self.ssl.model.eval()
        torch.onnx.export(
            self,
            (ref_audio_16k),
            f"onnx/{project_name}/{project_name}_cnhubert.onnx",
            input_names=["ref_audio_16k"],
            output_names=["last_hidden_state"],
            dynamic_axes={
                "ref_audio_16k": {1 : "text_length"},
                "last_hidden_state": {2 : "pred_length"}
            },
            opset_version=17,
            verbose=False
        )

def export(vits_path, gpt_path, project_name):
    vits = VitsModel(vits_path)
    gpt = T2SModel(gpt_path, vits)
    gpt_sovits = GptSoVits(vits, gpt)
    ssl = SSLModel()
    text_phones, word2ph, norm_text = text.cleaner.clean_text("Oh! I didn't realize you were there! Hello there, how are you doing today?", "en")
    ref_phones, word2ph2, norm_text2 = text.cleaner.clean_text(
    "We interrupt this broadcast with this urgent message. As of today, from advice from the health secretary, and other governing bodies",
"en")
    text_seq = torch.LongTensor([cleaned_text_to_sequence(text_phones)])
    ref_seq = torch.LongTensor([cleaned_text_to_sequence(ref_phones)])
    text_bert = torch.zeros((len(text_phones), 1024), dtype=torch.float32)
    ref_bert = torch.zeros((len(ref_phones), 1024), dtype=torch.float32)

    # ref_audio = torch.randn((1, 48000 * 5)).float()
    wav48k, sr = librosa.load("rec.wav", sr=48000)
    ref_audio = torch.tensor([wav48k]).float()
    ref_audio_16k = torchaudio.functional.resample(ref_audio, 48000, 16000).float()
    ref_audio_sr = torchaudio.functional.resample(ref_audio, 48000, vits.hps.data.sampling_rate).float()

    try:
        os.mkdir(f"onnx/{project_name}")
    except:
        pass

    ssl_content = ssl(ref_audio_16k).float()
    
    debug = True

    if debug:
        a, b = gpt_sovits(ref_seq, text_seq, ref_bert, text_bert, ref_audio_sr, ssl_content, debug=debug)
        soundfile.write("out1.wav", a.cpu().detach().numpy(), vits.hps.data.sampling_rate)
        soundfile.write("out2.wav", b[0], vits.hps.data.sampling_rate)
    
    # a = gpt_sovits(ref_seq, text_seq, ref_bert, text_bert, ref_audio_sr, ssl_content).detach().cpu().numpy()
    # soundfile.write("out.wav", a, vits.hps.data.sampling_rate)

    ssl.export(ref_audio_16k, project_name)
    gpt_sovits.export(ref_seq, text_seq, ref_bert, text_bert, ref_audio_sr, ssl_content, project_name)

    MoeVSConf = {
            "Folder" : f"{project_name}",
            "Name" : f"{project_name}",
            "Type" : "GPT-SoVits",
            "Rate" : vits.hps.data.sampling_rate,
            "NumLayers": gpt.t2s_model.num_layers,
            "EmbeddingDim": gpt.t2s_model.embedding_dim,
            "Dict": "BasicDict",
            "BertPath": "chinese-roberta-wwm-ext-large",
            "Symbol": symbols,
            "AddBlank": False
        }
    
    # MoeVSConfJson = json.dumps(MoeVSConf)
    # with open(f"onnx/{project_name}.json", 'w') as MoeVsConfFile:
    #    json.dump(MoeVSConf, MoeVsConfFile, indent = 4)


if __name__ == "__main__":
    try:
        os.mkdir("onnx")
    except:
        pass

    # gpt_path = "GPT_SoVITS/pretrained_models/v1/s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt"  # V1
    # vits_path = "GPT_SoVITS/pretrained_models/v1/s2G488k.pth"  # V1
    gpt_path = "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt"  # V2
    vits_path = "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s2G2333k.pth"  # V2
    exp_path = "nahida"
    export(vits_path, gpt_path, exp_path)

    # soundfile.write("out.wav", a, vits.hps.data.sampling_rate)