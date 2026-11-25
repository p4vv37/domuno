# Third-party notices — Domuno Pocket TTS

Domuno's MIT license covers its own code. It does not relicense the software,
model weights, tokenizer or reference recording used by the Wyoming-to-HTTP
bridge. These notices describe the current build, not older adapter revisions.

| Artifact | Source and terms |
| --- | --- |
| Pocket TTS Rust binary (`3.3.0`, Linux ARM64) | [Kyutai Labs, release v4.0.1](https://github.com/kyutai-labs/pocket-tts/releases/tag/v4.0.1), [MIT](https://github.com/kyutai-labs/pocket-tts/blob/v4.0.1/LICENSE). The image retains the license from the release archive at `/app/LICENSE-pocket-tts`. |
| Polish weights, tokenizer and configuration | [`shefowl/pocket-tts-polish-6l`, pinned model card](https://huggingface.co/shefowl/pocket-tts-polish-6l/blob/cd9047ee2fc9d1eb40ed38673d8b220485713863/README.md), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), derived from [Kyutai Pocket TTS weights](https://huggingface.co/kyutai/pocket-tts). Domuno rewrites the configuration's asset URLs to local paths; it does not modify the downloaded weights or tokenizer. |
| Polish `reference.wav` | The pinned card above identifies the supplied recording as a held-out clip from [CML-TTS](https://huggingface.co/datasets/ylacombe/cml-tts), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Domuno downloads it unchanged. Do not assume this attribution applies to a replacement voice; obtain the appropriate permission when using another person's voice. |
| English and Spanish weights, tokenizers and voice states (`alba`, `lola`) | [Public Pocket TTS release without voice cloning, Kyutai Labs](https://huggingface.co/kyutai/pocket-tts-without-voice-cloning), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) per the model card. Domuno downloads these files unchanged and uses upstream model configs with local asset paths. |
| `wyoming` 1.8.0 | [OHF-Voice Wyoming](https://github.com/OHF-Voice/wyoming/tree/v1.8.0), MIT. |
| `aiohttp` 3.14.5 | [aiohttp](https://pypi.org/project/aiohttp/3.14.5/), Apache-2.0 AND MIT per package metadata. |
| `num2words` 0.5.14 | [num2words](https://github.com/savoirfairelinux/num2words/tree/v0.5.14), LGPL-2.1; used for Polish number normalization. |

The standard image build copies the engine binary and installs the Python
packages. The model and reference recording are downloaded separately and
mounted as local assets. For image redistribution, use the standard Dockerfile
rather than a locally derived legacy image, retain upstream license texts and
notices for the base image and all dependencies, and check any applicable
source-distribution obligations. This list documents the direct TTS artifacts;
it is not a complete inventory of the image's transitive dependencies.
