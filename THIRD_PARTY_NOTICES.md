# Third-party notices

Omarchy Talks is MIT licensed. This file identifies important runtime and model boundaries; it does not replace the licenses or notices distributed by their respective projects.

| Component | Role in the supported path | Upstream license / source |
| --- | --- | --- |
| Jamie Pine VoiceBox | Pinned upstream local API, profiles, and inference | MIT; <https://github.com/jamiepine/voicebox> |
| Kokoro-82M model | English synthesis model used by the supported runtime | Apache-2.0; <https://huggingface.co/hexgrad/Kokoro-82M> |
| PyTorch | CPU tensor runtime | BSD-3-Clause; <https://github.com/pytorch/pytorch/blob/main/LICENSE> |
| FastAPI | VoiceBox HTTP API framework | MIT; <https://github.com/fastapi/fastapi/blob/master/LICENSE> |
| FastMCP, phonemizer/eSpeak, Misaki, and other resolved Python packages | Transitive or direct VoiceBox runtime dependencies | Their installed distribution metadata and upstream licenses apply. |

The supported install intentionally limits VoiceBox dependencies to the English Kokoro CPU path. It does not install or claim support for unverified language phonemizers or other VoiceBox engines. Consult each installed package and model's license before redistribution beyond this source release.
