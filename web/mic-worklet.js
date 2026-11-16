// Collects microphone samples into 10 ms Int16 chunks (480 samples at 48 kHz) for the PC.
class MicChunker extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = Math.round(sampleRate / 100);
    this.buf = new Int16Array(this.size);
    this.fill = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i++) {
      const s = Math.max(-1, Math.min(1, channel[i]));
      this.buf[this.fill++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.fill === this.size) {
        this.port.postMessage(this.buf.buffer, [this.buf.buffer]);
        this.buf = new Int16Array(this.size);
        this.fill = 0;
      }
    }
    return true;
  }
}

registerProcessor('mic-chunker', MicChunker);
