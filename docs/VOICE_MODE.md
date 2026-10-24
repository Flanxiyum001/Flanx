# Max Voice (wake-word mode)

Max Voice lets the configured owner ask Max to join a Discord voice channel, listen for
"Hey Max", transcribe speech locally, send only wake-word-authorized utterances to Max's
existing conversational model router, and speak replies using local Piper TTS.

## Privacy and safety defaults

- Voice mode is disabled unless MAX_VOICE_ENABLED=true.
- \`!max_voice\` and \`!max_voice_leave\` are restricted to the configured Max owner.
- Max joins the invoking owner's current voice channel and only processes PCM from that user.
- Audio buffers exist only in memory and are discarded after transcription; raw audio or transcripts are not persisted.
- Before a wake phrase is heard, speech is transcribed locally only to detect the wake phrase. Text is not sent to the chat model or posted to Discord.
- After "Hey Max", the conversation stays active for 25 seconds by default. Set MAX_VOICE_WAKE_WINDOW_SECONDS to change this.
- External actions retain existing owner checks and approval requirements. Spoken requests do not bypass those boundaries.
- The voice receive dependency is experimental and its API may change; test it in a private server.

## Install

Use Python 3.11+ and install the project's normal dependencies first. Then:

    python -m pip install -r requirements-voice.txt

Install FFmpeg and ensure ffmpeg is on PATH. Install Piper TTS and download a suitable local voice model.
Example voice download:

    python -m piper.download_voices en_US-lessac-medium

Keep the .onnx model and its matching .onnx.json config together. Set these variables in the environment
where Max runs:

- MAX_VOICE_ENABLED=true
- MAX_PIPER_MODEL=/absolute/path/to/en_US-lessac-medium.onnx
- MAX_STT_MODEL=base.en (default; first use downloads the model)
- MAX_STT_DEVICE=cpu (default; use cuda only with compatible CTranslate2/CUDA/cuDNN)
- MAX_STT_COMPUTE_TYPE=int8 for CPU, or float16 for a supported CUDA setup
- MAX_VOICE_RMS_THRESHOLD=420 (optional speech/silence threshold)
- MAX_VOICE_WAKE_WINDOW_SECONDS=25 (optional follow-up window)

For Windows, use an absolute path such as C:\Max\models\en_US-lessac-medium.onnx. Ensure the model is
downloaded before starting Max if the host has no outbound network access. The bot needs Connect and
Speak permissions in the target channel. The existing Discord Message Content Intent is still required
for text commands.

## Use

1. Join a voice channel.
2. In a text channel, run \`!max_voice\`.
3. Say "Hey Max, ..." in voice.
4. Speak again within the wake window for follow-up turns.
5. Run \`!max_voice_leave\` to stop listening and disconnect.

\`!max_join\` and \`!max_leave\` are aliases. Max stops current playback when new speech from the
authorized speaker begins. Audio from other members is ignored.

## Troubleshooting

- If voice mode is disabled, set MAX_VOICE_ENABLED=true and restart.
- If it cannot start, check discord-ext-voice-recv, PyNaCl, and the bot's Connect/Speak permissions.
- If Piper cannot synthesize, verify MAX_PIPER_MODEL, the Piper executable, and voice model files.
- If transcription is slow, use a smaller Whisper model such as tiny.en or base.en.
- If no wake word is detected, adjust MAX_VOICE_RMS_THRESHOLD; quiet microphones may need a lower value.
- Railway needs voice-receive support, FFmpeg, and model files. A model on your PC is not automatically
  available to Railway. For local-first use, run Max on your PC or use a securely reachable inference host.
  Do not expose an unauthenticated local service to the internet.
