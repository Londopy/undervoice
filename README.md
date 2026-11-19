# undervoice

**Send your callsign, grid square and a short status during an ordinary FM voice QSO, without
anyone stopping to send them.**

undervoice mixes a slow spread-spectrum data signal into your mic audio, well below the speech
and shaped to follow it, so it rides through an unmodified FM radio and a sound card interface
(the same kind you would use for VARA FM). The receiving station pulls the frames back out of
its speaker audio. One frame carries a callsign, a grid square and 32 characters of status every
9.3 seconds, about 27 bit/s of payload.

This is a prototype. It works through the simulated FM channel below and through a sound card
loopback. It has not been on the air yet, and nobody has done the blind listening test.

## Install

From a clone, with Python 3.10 or newer:

```powershell
pip install -e .[live]
```

`live` adds [sounddevice](https://pypi.org/project/sounddevice/) for sound card use. Leave it off
if you only want to work with WAV files.

## Usage

Mix data under a recording and decode it again:

```powershell
undervoice encode speech.wav mixed.wav --call W1AW --grid FN31pr --status "ON THE AIR"
undervoice decode mixed.wav
```

Run it through the simulated FM channel first (`--hops 2` goes through a repeater, `--ppm` sets
a sound card clock offset):

```powershell
undervoice channel mixed.wav received.wav --snr 35 --hops 2 --ppm 80
undervoice decode received.wav
```

Measure frame loss against data level over a set of speech recordings:

```powershell
undervoice sweep speech/*.wav --levels=-27,-24,-21,-18 --trials 3 --snr 40
```

On the air, with the radio's audio on a sound card (`undervoice devices` lists them):

```powershell
undervoice tx --call W1AW --grid FN31pr --status "MOBILE QRV" --input "Microphone" --output "USB Audio"
undervoice tx --play speech.wav --call W1AW --output "USB Audio"
undervoice rx --input "USB Audio"
```

`tx` takes your mic, adds the data and sends the result to the radio's audio input; key the radio
with its own PTT or VOX as usual. `--play` sends a recording instead of the mic. `rx` prints
each frame as it is heard, stamped in UTC, warns when the sound card input clips, and with
`--record session.wav` also saves what it heard so you can run `decode` on it later. `--level`
(dB relative to the speech) and `--shape` work on every command that sends; `--shape` must
match on both ends.

## Results so far

Frame loss through the simulated channel, four Harvard-sentence recordings, each sent three
times from random start points (36 frames per cell). "40 dB" means a full-deviation 1 kHz tone
is 40 dB above the noise at the receiver, which leaves speech about 22 dB above it; at 30 dB the
hiss is plainly audible. Not one frame in any run decoded to the wrong message.

`--shape 0.5` (the default):

| Level | 40 dB simplex | 30 dB simplex | 40 dB via repeater | 40 dB, 100 ppm clock offset |
|---|---|---|---|---|
| -27 dB | 33% | 100% | 94% | 42% |
| -24 dB | 6% | 100% | 31% | 8% |
| -21 dB | 0% | 67% | 0% | 0% |
| -18 dB | 0% | 22% | 0% | 0% |
| -15 dB | 0% | 0% | 0% | 0% |

`--shape 0` (flat data, easier to decode, probably easier to hear):

| Level | 40 dB simplex | 30 dB simplex | 40 dB via repeater | 40 dB, 100 ppm clock offset |
|---|---|---|---|---|
| -27 dB | 19% | 100% | 89% | 22% |
| -24 dB | 0% | 92% | 28% | 0% |
| -21 dB | 0% | 42% | 0% | 0% |
| -18 dB | 0% | 14% | 0% | 0% |
| -15 dB | 0% | 0% | 0% | 0% |

With `--shape 0.9` the data follows the speech spectrum closely and needs about -18 dB even on
a clean 40 dB path. The default, shape 0.5 at -18 dB, keeps a 3 dB margin on a good simplex or
repeater path and sits between the two in how much it should stand out. Whether any of these
can be heard is what the listening test has to answer.

Over a real sound card (a VB-Audio virtual cable from `tx --play` to `rx`) every frame came
through.

## Format

Everything needed to build a compatible encoder or decoder is here, as Part 97 requires for a
data emission.

**Audio.** 8000 samples per second, mono. Every symbol is 100 samples (80 baud).

**Payload.** 256 bits, most significant bit first:

| Bits | Field |
|---|---|
| 2 | version, 0 |
| 54 | callsign, 10 characters |
| 25 | grid square |
| 171 | status, 32 characters |
| 4 | reserved, 0 |

Text uses the 40-character alphabet ` ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-.` (space is index 0),
right-padded with spaces and read as one base-40 number, first character most significant.
A 6-character grid `AAnnaa` is
`((((f1*18 + f2)*10 + n1)*10 + n2)*24 + s1)*24 + s2`, with fields and subsquares counted from A=0.
A 4-character grid is `18662400 + ((f1*18 + f2)*10 + n1)*10 + n2`. No grid is `2^25 - 1`.
The 32 payload bytes are followed by CRC-16/CCITT-FALSE (polynomial 0x1021, initial value
0xFFFF) over them, big-endian, for 34 bytes in all.

**Coding.** The 272 bits plus 7 zero tail bits go through a rate 1/2, K=7 convolutional code with
generators 171 and 133 octal (the register's newest bit is its most significant), giving 558
coded bits, two per input bit with the 171 output first. Coded bit `i` is sent in data slot
`(37 * i) mod 558`.

**Frame.** 744 symbols (74400 samples, 9.3 s). Symbol `k` is a pilot when `k mod 4 == 0` and
carries +1. The other 558 symbols, in order, are the data slots, carrying `+1` for a 0 bit and
`-1` for a 1 bit. Frames repeat back to back for as long as there is audio.

**Chips.** For each frame a 17-bit shift register starts at all ones. For each of the 74400
samples, the chip is `+1` if bit 16 is 0 and `-1` if it is 1, then the register shifts left
and takes `bit16 XOR bit13` as its new bit 0 (x^17 + x^14 + 1, period 131071). The chips go
through a 129-tap Hamming-window FIR bandpass, 300 to 3000 Hz (`scipy.signal.firwin(129,
[300, 3000], pass_zero=False, fs=8000)`), centered so it adds no delay, and are scaled to unit
RMS. Sample `n` of the data signal is that filtered chip times the sign of the symbol it falls in.

**Shaping.** For every 20 ms (160 samples) of speech the transmitter finds the 12th-order LPC
filter A(z) of that block and the one before it (320 samples, Hann window, autocorrelation
method), runs the data through 1/A(z/shape), and scales it so its RMS is `level` dB below the
speech's RMS for the block, ramping the gain linearly across the block. Silence carries no data.
`shape` is 0.0 for flat data and approaches 1.0 as the data follows the speech spectrum more
closely.

## How the receiver works

1. Whiten the received audio with its own short-term LPC filter, which takes out most of the
   speech while leaving the data correlatable.
2. Correlate twelve blocks of pilot symbols against the whitened audio and add up their peaks
   to find frame starts. Each block's peak position gives a timing point; a line through them
   gives the start and the clock offset between the two sound cards.
3. Resample the frame onto the transmitter's clock and correlate each symbol against the
   reference, filtered the same way the data was, in four sub-bands (300-700, 700-1200,
   1200-1900 and 1900-3000 Hz).
4. In each band, take the phase and signal level from nearby pilots and the noise from the
   quadrature component, then add the bands' soft values together, so bands where channel
   noise drowns the data count for little.
5. De-interleave, Viterbi-decode, and keep the frame only if the CRC, version and every field
   check out.

## Testing on the air

**Simplex first**, at low power on a quiet 2 m or 70 cm simplex frequency, with your callsign
by voice as usual:

1. On the receiving station, set the radio's volume so speech peaks sit well below full scale
   on the sound card, then start `undervoice rx --input "USB Audio" --record simplex1.wav`.
   If it warns that the input clips, turn the input level down.
2. On the sending station, `undervoice tx --play speech.wav --call <you> --output "USB Audio"`
   with the radio keyed for the whole recording. A recording gives the same audio every run;
   move to the live mic once that works.
3. Each frame needs 9.3 s of transmission, so expect one frame per 9.3 s keyed. Note how many
   came through, and keep the recording: `undervoice decode simplex1.wav` replays the session
   offline, and the file is what to compare against the simulator if the radio does worse.
4. Step `--level` down from -15 dB until frames start to drop. That gives the real margin to
   compare with the simulated tables above.

**Then through a repeater**, only with its owner's permission. Repeaters often run their own
audio processing, so repeat the simplex steps through it, record on the receiving side, and
compare loss at the same levels. A repeater that regenerates CTCSS or runs its own limiter is
the case the simulator models least well. Tell the owner the format is published here and
that it carries your callsign in every frame.

## Rules (US, Part 97)

Read against the current eCFR text of 47 CFR Part 97 in November 2026. This is a reading of
the rules, not legal advice; check with ARRL or the FCC before relying on it.

- **The bands allow it.** On 2 m above 144.1 MHz and on all of 70 cm, 97.305(c) authorizes
  phone and data together ("MCW, phone, image, RTTY, data, test", plus SS on 70 cm). Phone and
  data sub-bands there come from voluntary band plans, not from the rules.
- **Mixed emissions are named.** 97.307(f)(5) (2 m) and (f)(6) (70 cm) allow "a RTTY, data or
  multiplexed emission", and 97.307(f)(8) also authorizes data emissions whose designator has W
  as the third symbol, the ITU symbol for a combination of information types. FM voice with
  this data under it is F9W: 9 is the ITU second symbol for analog and digital channels
  together, and (f)(8) lists it. The data is 80 baud against limits of
  19.6 kbaud (2 m) and 56 kbaud (70 cm), and it stays inside the bandwidth of the FM voice it
  rides on, as 97.307(f)(2) requires.
- **The code is covered by publication.** 97.309(a)(4) counts a code as specified when its
  "technical characteristics have been documented publicly". The Format section above is that
  documentation. 97.309(b) separately allows unspecified codes on these bands as long as they
  are not used to obscure meaning, and lets an FCC Regional Director require a record of
  traffic, so keep the `--record` files.
- **Nothing is hidden in meaning.** 97.113(a)(4) bans messages encoded to obscure their
  meaning. The data is unencrypted, the format and code are public, and every frame carries
  the callsign in clear. Inaudible is not the same as obscured, but this is the point a
  skeptical reader is most likely to raise, so lead with the published format.
- **Identify by voice as usual.** 97.119 requires your callsign every 10 minutes and at the
  end, by CW or phone. Nothing in it lets the callsign in the data count as identification.
- **Repeaters.** 97.205(g) says the control operator of a repeater that inadvertently
  retransmits something that breaks the rules is not accountable for it, but the repeater's
  licensee can still limit who and what uses it (97.205(e)). Ask first.

Still open, worth putting to ARRL: whether the FCC would treat this as phone, data or a
combined emission in practice; and whether an unscreened 32-character status needs the same
care as any other text under 97.113 (no business, no obscenity).

## Known gaps

- Silence carries no data, so a frame that spans long pauses can be lost. A minimum level
  during pauses would fix this at the cost of being audible.
- The listening test is published but has no answers yet; the simplex and repeater tests have
  not been done.
- `tx` starts frames when it starts and runs them back to back; a frame cut off by unkeying is
  lost.

## Building from source

```powershell
git clone https://github.com/Londopy/undervoice
cd undervoice
pip install -e .[live,dev]
pytest
```

## Speech recordings

The WAV files in `speech/` are Harvard sentences from the
[Open Speech Repository](https://www.voiptroubleshooter.com/open_speech/), used for the tests and
sweeps.

## License

MIT. See [LICENSE](LICENSE).
