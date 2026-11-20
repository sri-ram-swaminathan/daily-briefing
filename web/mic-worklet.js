// Captures microphone audio, downsamples to 16 kHz mono PCM16, and posts 20 ms frames
// together with their RMS level (used by the voice-activity detector on the main thread).
class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.frame = new Int16Array(320); // 20 ms at 16 kHz
    this.fill = 0;
    this.pos = 0; // fractional read position for resampling
    this.sumSq = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    // simple averaging decimator (fine for speech)
    while (this.pos < input.length) {
      const start = Math.floor(this.pos);
      const end = Math.min(input.length, Math.floor(this.pos + this.ratio));
      let acc = 0;
      for (let i = start; i < Math.max(end, start + 1); i++) acc += input[i];
      const s = Math.max(-1, Math.min(1, acc / Math.max(1, end - start)));
      this.frame[this.fill++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      this.sumSq += s * s;
      this.pos += this.ratio;
      if (this.fill === this.frame.length) {
        const rms = Math.sqrt(this.sumSq / this.frame.length);
        this.port.postMessage({ pcm: this.frame.buffer.slice(0), rms });
        this.fill = 0;
        this.sumSq = 0;
      }
    }
    this.pos -= input.length;
    return true;
  }
}

registerProcessor("mic-processor", MicProcessor);
