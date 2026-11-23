# undervoice

[![CI](https://github.com/Londopy/undervoice/actions/workflows/ci.yml/badge.svg)](https://github.com/Londopy/undervoice/actions/workflows/ci.yml)
[![changelog](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Londopy/undervoice/gh-pages/changelog-badge.json)](CHANGELOG.md)
[![Release](https://img.shields.io/github/v/release/Londopy/undervoice)](https://github.com/Londopy/undervoice/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/undervoice)](https://pypi.org/project/undervoice/)
[![Python](https://img.shields.io/pypi/pyversions/undervoice)](https://pypi.org/project/undervoice/)
[![Downloads](https://img.shields.io/github/downloads/Londopy/undervoice/total)](https://github.com/Londopy/undervoice/releases)
[![License](https://img.shields.io/github/license/Londopy/undervoice)](LICENSE)
[![Windows](https://img.shields.io/badge/platform-Windows%2010%2B-0078D6)](#install)

**Send your callsign, grid square and a short status during an ordinary FM voice QSO, without
anyone stopping to send them.**

undervoice mixes a slow spread-spectrum data signal into your mic audio, well below the speech
and shaped to follow it, so it rides through an unmodified FM radio and a sound card interface
(the same kind you would use for VARA FM). The receiving station pulls the frames back out of
its speaker audio. One frame carries a callsign, a grid square and 32 characters of status every
9.3 seconds, about 27 bit/s of payload. It can also send a short text message to another
station, 24 characters a frame.

This is a prototype. It works through the simulated FM channel below and through a sound card
loopback. It has not been on the air yet, and nobody has done the blind listening test.

## Install

**Windows:** every [release](https://github.com/Londopy/undervoice/releases/latest) has the same
downloads; pick whichever you like.

| Download | What it is |
|---|---|
| `undervoice-<version>-setup-x64.exe` | **Installer (recommended).** Per-user, no admin prompt. |
| `undervoice-<version>-x64.msi` | MSI installer. Per-user by default; IT can deploy it machine-wide. |
| `undervoice-v<version>-x64-portable.exe` | One exe, nothing to install. It unpacks itself on every start, so it opens in about ten seconds where the installed app takes two. |
| `undervoice-v<version>-portable.zip` | The portable exe in a folder; settings stay next to it. |
| `install.ps1` / `install.cmd` | Script install that picks a download and checks its SHA-256. |
| `*.torrent` / magnet links | Each download over BitTorrent, seeded from GitHub so it works with no peers. |

The Windows downloads are the window (`undervoice gui`) built for 64-bit Windows 10 and 11;
Windows on ARM runs them under emulation. For the command line, install with pip below.

**PowerShell one-liner:**

```powershell
irm https://github.com/Londopy/undervoice/releases/latest/download/install.ps1 | iex
```

Options: `-Msi`, `-Portable`, `-AllUsers`, `-Version 0.2.0`, `-NoShortcut`, `-DryRun`. To pass
them, run the script as a block:
`& ([scriptblock]::Create((irm https://github.com/Londopy/undervoice/releases/latest/download/install.ps1))) -Portable`

**Scoop:**

```powershell
scoop bucket add londopy https://github.com/Londopy/scoop-bucket
scoop install londopy/undervoice
```

**MSI for every user** (admin): `msiexec /i undervoice-<version>-x64.msi ALLUSERS=2 MSIINSTALLPERUSER=""`.
Add `DESKTOPSHORTCUT=0` to skip the desktop icon.

The downloads are not code-signed, so Windows SmartScreen may warn on first run (**More info**,
then **Run anyway**). Every release ships `SHA256SUMS.txt`; check a download with:

```powershell
(Get-FileHash .\<file> -Algorithm SHA256).Hash
```

**Any platform, with Python 3.11 or newer:**

```bash
pip install undervoice[live]
```

That gives the `undervoice` command and `undervoice-gui`, which opens the window without a
console. `live` adds sound card and PTT support (sounddevice, hidapi, pyserial); leave it off
if you only work with WAV files.

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
with its own PTT or VOX, or let undervoice key it (below). `--play` sends a recording instead of the mic. `rx` prints
each frame as it is heard, stamped in UTC, warns when the sound card input clips, and with
`--record session.wav` also saves what it heard so you can run `decode` on it later. `--level`
(dB relative to the speech) and `--shape` work on every command that sends; `--shape` must
match on both ends. In the sub-audible mode (below) `--level` is relative to full scale
instead, and `--shape` and `--pause-level` do nothing.

### The window

```powershell
undervoice gui
```

or `undervoice-gui`, which opens without a console window on Windows. Fill in your callsign,
grid and status, pick the microphone, the sound card going to the radio and the one coming
from it, and the PTT interface if you have one. **Transmit** keys the radio (through PTT, or
you key it yourself) and mixes the data into your microphone audio until you press it again
or hit Esc. Or hold **Space** to talk and let go to stop, like a PTT button (buttons take Enter
instead of Space in this window). **Send a recording...** does the same with a WAV file. The window starts
listening as soon as it opens (untick **Listen whenever undervoice is open** to start it by
hand with **Listen**) and fills the Received table with each frame as it decodes, newest
first. It shows the input level so you can set the radio's volume, and can record the session
to a file. The window follows Windows' light or dark setting.

### Checking your setup

Before going on the air, the window's **Test microphone** button (or `undervoice check`) records
about 11 seconds of you talking, with the input meter showing the level, then:

- says whether the microphone level is good, too quiet or clipping;
- mixes your data into the recording as a transmission would and decodes it again in memory,
  which checks the microphone, its level, and the encoder and decoder at your settings, but
  not the sound card going to the radio or the radio itself;
- plays the result on the system's default output, so you hear what would go to the radio,
  data and all. If that default output is a VOX radio interface such as a SignaLink, this
  transmits: make the PC's speakers the default output first.

**Test PTT** (or `undervoice check --ptt ...`) keys the radio for 2 seconds through the PTT
method you picked, so you can see the transmit light come on. It really transmits: use a dummy
load or a clear frequency, and identify as usual.

```powershell
undervoice check --call W1AW --input "Headset" --ptt COM5
```

### Two modes

**Under the voice** (the default) is everything described above: data hidden in the speech's
own band, so it goes wherever the voice goes, repeaters included.

**Sub-audible** (`--mode sub`, or **Mode** in the window) carries the same payload below
300 Hz, where CTCSS tones live, at a constant level. A receiver's audio high-pass, there to
keep CTCSS tones out of the speaker, cuts it down: the simulator's 4th-order 300 Hz high-pass
takes it about 20 dB down. How far a real radio's filter takes it hasn't been measured. It is
faster (a frame every 5.9 s, about 43 bit/s of payload) and never depends on the speech, so
pauses cost nothing. The
catches: it needs the radio's flat 9600-baud data port (set the D710G's data speed to 9600 for
this mode), CTCSS and DCS must be off on that channel, and most repeaters strip or regenerate
everything below 300 Hz, so in practice it is for simplex. Both ends must use the same mode;
changing **Mode** in the window while it listens starts listening again in the new mode. The
format is under Format below.

### Messages

Besides the station frame, undervoice can send a text message to one station, or to everyone
when **To** is left blank. Messages are capitals, digits, space and `/ - . ? , ! ' :`, up to
384 characters, 24 to a frame:

```powershell
undervoice tx --call W1AW --to N0CALL --msg "Meet you on 146.52 at 7?" --input "Microphone" --output "USB Audio"
```

The parts go out in order in place of the station frame, then the station frame carries on. That
takes time: a frame is 9.3 s of talking under the voice (5.9 s of transmitting in sub-audible
mode), so a 100-character message needs about 47 s of talk. Under the voice the data only goes
while you speak, unless **In pauses** is set. A part cut off when you unkey goes again at the
start of your next over. In the window, type in **Messages** and press **Send**; the counter
shows how long it will take, and the parts wait for your next over if you aren't transmitting.
**Send again** sends the same message under the same number, so a station that missed a part
fills the gap, and **Cancel** drops what hasn't gone.

`rx` and the window put the parts back together. A message heard in part shows `[...]` where a
part is missing; messages to your callsign are highlighted in the window. Nothing acknowledges a
message: the other station answers on voice, or with a message of its own. Stations on 0.5.0 or
older ignore message frames.

To keep a record, tick **Save messages to a text file** in the window, or give `tx` and `rx`
`--messages messages.txt`. Each message, sent or heard, is added as one line:

```text
2026-11-20 07:12  W1AW > N0CALL  MEET YOU ON 146.52 AT 7?
2026-11-20 07:15  N0CALL > W1AW  SEE YOU T[...]  (missing part 2 of 2)
```

A heard message is written when its last part arrives, or as it stands when `rx` stops or the
window closes. A sent message is written once its last part has gone out.

### Keying the radio

`--ptt` (or the PTT box in the window) keys the radio four ways, which between them cover the
common interfaces. `undervoice devices` lists what this machine offers.

| `--ptt` | Keys through | For |
|---|---|---|
| `cm108`, `cm108:N` | a GPIO pin on the interface's USB sound chip | DMK URI, RIM-Lite, AIOC and other CM108/CM119 interfaces |
| `COM5`, `COM5:dtr`, `/dev/ttyUSB0` | a serial port's RTS line, or DTR | Digirig and most interfaces with a COM port |
| `rigctld`, `rigctld:host:port` | a CAT command through a running Hamlib `rigctld` | any rig Hamlib supports, over its CAT cable |
| left out | nothing: the interface or you key the radio | SignaLink and other VOX interfaces, or keying by hand |

```powershell
undervoice tx --play speech.wav --call W1AW --output "USB Audio" --ptt
undervoice tx --call W1AW --input "Headset" --output "USB Audio" --ptt COM5
```

With `--play` the radio is keyed for the whole recording. With the mic, press Enter to key and
Enter again to unkey; each transmission starts a fresh frame, so none is cut off by unkeying
mid-frame unless the transmission itself is shorter than 9.3 s. A fresh frame also starts
whenever you start talking again after 3 s of silence, so overs keyed by the radio's own PTT
or a VOX interface line up with frames too. `--txdelay` (default 0.3 s) is
how long the radio gets to come up to power before audio starts. CM108 interfaces usually wire
PTT to GPIO 3; use `--ptt-gpio` if yours differs. The serial port's lines are held low while it
opens, so opening it does not key the radio. On Linux the user needs write access to the
interface's `/dev/hidraw*` or serial device, usually through a udev rule or the `dialout`
group.

VOX interfaces key on audio, so they key whenever the microphone carries sound and can drop
out during long pauses (turn up the SignaLink's DELAY knob), and they clip the first moment
of each transmission while they switch.

When the radio is fed through its data port (the 6-pin mini-DIN on most mobiles), it ignores
its own front-panel mic while transmitting, so speak into a mic on the PC. On radios with a
1200/9600 data speed setting, use 1200: its receive audio is filtered and de-emphasized like
the speaker audio undervoice expects.

### Settings and updates

The window remembers your station, sound cards, PTT and levels between runs, in `config.toml`:

| | Path |
|---|---|
| Windows | `%APPDATA%\undervoice\config.toml` |
| Portable copy | `config.toml` next to the exe (a file named `portable` beside it switches this on) |
| macOS | `~/Library/Application Support/undervoice/config.toml` |
| Linux | `~/.config/undervoice/config.toml` |

```toml
[station]
call = "W1AW"
grid = "FN31pr"
status = "MOBILE QRV"

[radio]
ptt = "COM5"            # the same values --ptt takes, or "" for VOX or keying by hand
level = "-18"
shape = "0.5"
txdelay = "0.3"

[updater]
enabled = true
channel = "stable"      # "stable" or "beta" (includes pre-releases)
check_interval_h = 24
```

The Updates row checks GitHub Releases on that schedule and on **Check now**. When there is a
newer version it offers **Download and install**; nothing installs until you click it. The
download is checked against the release's `SHA256SUMS.txt` first and refused on a mismatch.
An installed copy updates through the same installer it came from; a portable exe replaces
itself. A copy installed with pip says so and leaves the update to
`pip install -U undervoice[live]`.

## Results so far

All of these are measured through the simulated FM channel (`undervoice sweep`), not on the
air. "40 dB" means a full-deviation 1 kHz tone is 40 dB above the noise at the receiver, which
leaves these recordings' speech 22 to 28 dB above it, depending on the recording, and about
10 dB less at "30 dB". Not one frame in any run decoded to the wrong message.

**The spec's target, under 1% frame loss on a clean simplex path:** at the defaults (`--level
-18 --shape 0.5`, 40 dB, simplex), 480 frames out of 480 came through. That rules out a loss
rate above 0.62% with 95% confidence (one-sided Clopper-Pearson, which `sweep` prints as
`loss_95_upper_pct`). `pytest -m slow` repeats this measurement, and the Targets workflow runs
it weekly.

The tables below are smaller runs (36 frames per cell, four recordings sent three times each
from random start points), good for comparing settings but too small on their own to show a
rate under 1%.

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

**Pauses.** The data follows the speech level, so it stops when you stop talking, and a frame
that spans a long pause loses those symbols. `--pause-level` (or **In pauses** in the window)
keeps it going through pauses at that many dB below the recent speech instead; it can't be
above the data level itself. On the noisy 30 dB path, 96 frames per cell, from
`undervoice sweep "speech/*.wav" --levels=-21,-18 --trials 8 --snr 30 --seed 2` with each
`--pause-level`; other seeds move these by a few percent, as 96 frames is not many:

| `--pause-level` | at -21 dB | at -18 dB |
|---|---|---|
| off (default) | 74% lost | 27% lost |
| -40 | 21% | 12.5% |
| -34 | 3% | 0% |
| -28 | 0% | 0% |

It is off by default because during a pause there is no speech to hide the data, so how high
it can go before listeners hear it is for the listening test to settle. On a clean path it
makes no difference, since frames already come through.

**Sub-audible mode**, through the simulated flat data-port path (`channel --path flat`), 20
frames per cell; the level is the data's power relative to a full-scale sine:

| Level | 40 dB | 30 dB | 20 dB | 10 dB |
|---|---|---|---|---|
| -48 dB | 0% | 0% | 90% | 100% |
| -42 dB | 0% | 0% | 0% | 100% |
| -36 dB | 0% | 0% | 0% | 0% |
| -18 dB (default) | 0% | 0% | 0% | 0% |

The simulator has no FM threshold clicks, so the 10 dB column flatters it. At the default it
also decoded every frame with a 1000 ppm clock offset, a large DC offset, mains hum up to
-24 dBFS and a 200 Hz high-pass on the receiving side. A CTCSS tone on the sending radio is
what breaks it: at 15% deviation, every standard tone from 107.2 to 192.8 Hz stops it
completely, and the lower and higher ones still cost the odd frame, so turn tones off for this
mode. Through a repeater it decoded nothing.

A manual check through a VB-Audio virtual cable, from `tx --play` to `rx`, decoded every frame
sent. No log of it was kept, and a virtual cable is not a real sound card.

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
A message part has version 1 and this layout instead:

| Bits | Field |
|---|---|
| 2 | version, 1 |
| 54 | from callsign, 10 characters |
| 54 | to callsign, 10 characters, all spaces for everyone |
| 6 | message number, 0 to 63, the same for every part of one message |
| 4 | part index, from 0 |
| 4 | number of parts minus 1 |
| 132 | text, 24 characters |

Message text uses the 40-character alphabet above followed by `?,!':`, 45 characters, read as
one base-45 number the same way. Every part but the last carries exactly 24 characters, spaces
included; the last is right-padded with spaces. A receiver joins parts with the same from
callsign and message number; a part that doesn't fit the ones heard (other text for the same
index, another number of parts or to callsign, or more than an hour later) starts a new
message. Receivers ignore versions they don't know.

The 32 payload bytes are followed by CRC-16/CCITT-FALSE (polynomial 0x1021, initial value
0xFFFF) over them, big-endian, for 34 bytes in all.

**Coding.** The 272 bits plus 7 zero tail bits go through a rate 1/2, K=7 convolutional code with
generators 171 and 133 octal (the register's newest bit is its most significant), giving 558
coded bits, two per input bit with the 171 output first. Coded bit `i` is sent in data slot
`(37 * i) mod 558`.

**Frame.** 744 symbols (74400 samples, 9.3 s). Symbol `k` is a pilot when `k mod 4 == 0` and
carries +1. The other 558 symbols, in order, are the data slots, carrying `+1` for a 0 bit and
`-1` for a 1 bit. Frames repeat back to back for as long as there is audio: any message parts
waiting to go, in order, then the station frame again and again.

**Chips.** For each frame a 17-bit shift register starts at all ones. For each of the 74400
samples, the chip is `+1` if bit 16 is 0 and `-1` if it is 1, then the register shifts left
and takes `bit16 XOR bit13` as its new bit 0 (x^17 + x^14 + 1, period 131071). The chips go
through a 129-tap Hamming-window FIR bandpass, 300 to 3000 Hz (`scipy.signal.firwin(129,
[300, 3000], pass_zero=False, fs=8000)`), centered so it adds no delay, and are scaled to unit
RMS. Sample `n` of the data signal is that filtered chip times the sign of the symbol it falls in.

**Shaping.** For every 20 ms (160 samples) of speech the transmitter finds the 12th-order LPC
filter A(z) of that block and the one before it (320 samples, Hann window, autocorrelation
method), runs the data through 1/A(z/shape), and scales it so its RMS is `level` dB below the
speech's RMS for the block, ramping the gain linearly across the block. Silence carries no data,
unless a pause level is set: then the target RMS is never less than `pause` dB below the
recent speech level, where the recent level is the block RMS held at its peak and multiplied by
0.999 every block. `shape` is 0.0 for flat data and approaches 1.0 as the data follows the
speech spectrum more closely.

### Format: sub-audible mode

**Audio.** 8000 samples per second, mono. Every symbol is 80 samples (100 baud).

**Payload and coding.** The same 34-byte payload and the same code as above: 558 coded bits,
coded bit `i` in data slot `(37 * i) mod 558`.

**Frame.** 589 symbols (47120 samples, 5.89 s): 31 sync symbols carrying
`1100011111001101001000010101110`, first bit first, then the 558 data slots in order. Frames
repeat back to back for as long as there is audio. The sync word is an m-sequence
(`b[n+5] = b[n+3] XOR b[n]`) in the rotation whose aperiodic autocorrelation sidelobes are no
more than 4.

**Phase changes.** Every bit sets how the carrier phase changes from the symbol before: a 0
keeps it and a 1 flips it. With `a[k]` the sign of symbol `k`, `a[k] = a[k-1]` for a 0 and
`-a[k-1]` for a 1. The signs carry on from frame to frame; the first symbol of a transmission
follows an implied `+1`.

**Pulse.** Symbol `k` of a frame that starts at sample `s` adds `a[k] * h[n - s - 80k]` to the
baseband signal, where `h[m] = sinc(2t - 1/2) + sinc(2t + 1/2)` at `t = (m - 320) / 80` for `m`
from 0 to 640, and `sinc(x) = sin(pi x) / (pi x)`. That is a root-raised-cosine pulse with
roll-off 1, cut off four symbols either side of its peak, scaled so the squares of `h` sum to
80. Each pulse starts where its symbol starts and peaks 320 samples later; neighbouring pulses
overlap.

**Carrier.** The baseband signal times `cos(2 pi * 150 * n / 8000)`, with `n` counting samples
from the start of the transmission; the receiver does not need its phase. The signal sits
between 50 and 250 Hz, and its spectrum everywhere above 300 Hz is at least 55 dB below its
peak (a test checks this).

**Rate.** 100 symbols a second on the air, close to the 134 bit/s that DCS sends in the same
band. The rate 1/2 code and the 31-symbol sync word leave 256
payload bits every 5.89 s, about 43 bit/s. Going faster would mean either more symbols a
second, which would spread the signal past 300 Hz into the voice, or more bits per symbol,
which needs more signal-to-noise than a low constant level below the voice band has to give.

**Level.** The data is scaled by `10^(level/20)`, which gives it the power of a sine of that
peak amplitude relative to full scale. It stays constant and does not follow the speech. The
default, -18 dB, has the power of a CTCSS tone at 12.6% of full deviation when full scale is
full deviation, and peaks at about 19%.

**Speech.** Before the data is added, the speech goes through an 8th-order elliptic high-pass,
0.5 dB ripple, 60 dB stopband, passband from 300 Hz
(`scipy.signal.ellip(8, 0.5, 60, 300, "highpass", fs=8000, output="sos")`), which puts the
voice at least 60 dB down below 260 Hz, then a pre-emphasis, because the data port bypasses
the radio's own mic filter and emphasis. The pre-emphasis is the bilinear transform at
8000 Hz of `(1 + 750e-6 s) / (1 + s / (2 pi 3500))`
(`scipy.signal.bilinear([750e-6, 1], [1 / (2 * pi * 3500), 1], fs=8000)`), scaled to unity
gain at 1 kHz. It rises 6 dB per octave like a radio's 750 us network, within 0.2 dB of it
from 300 Hz to 3 kHz (+9.4 dB at 3 kHz), then levels off, reaching +10.6 dB at 3.9 kHz.

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

In the sub-audible mode the receiver low-passes the audio at 275 Hz, mixes it down from
150 Hz and runs it through the same pulse as a matched filter. Multiplying that output by its
own conjugate one symbol earlier gives the phase change between two symbols at every sample,
whatever the carrier phase and whichever way up the audio is. The sync word is correlated
against those products at every sample offset; every peak above 0.45 (a perfect match is 1)
is tried as a frame start. Symbol timing comes from the symbol-rate line in the squared filter
output, measured in 19 blocks and fitted with a straight line for the clock offset, and the
real part of each phase change over the local noise is the soft value for the Viterbi decoder.

## Design choices

**Both variants from the plan are here.** The plan offered two ways to carry the data: under
the voice (the default mode) and sub-audible below 300 Hz. Sub-audible is faster and immune to
pauses but needs a flat data port, tone-free channels and, in practice, simplex; under the voice
goes anywhere speech goes. The simulator agrees: through ordinary mic and speaker audio the
sub-audible mode does not decode reliably at the default level, even on a single simplex link,
and through a repeater (two links, four 300 Hz high-pass filters) it decodes nothing. It needs
the flat data port.

**Long frames with pilots, not short frames with a sync word.** The original plan called for
short frames with a sync word, because speech and squelch cut in and out. Hidden under speech
the channel carries about 60 coded bits a second, so the 34-byte payload with its error
correction needs about 9 seconds however it is cut up; short frames would mean splitting the
callsign, grid and status across several frames, each with its own sync and CRC overhead.
Instead one frame carries everything, and the job a sync word would do is spread through it:
every fourth symbol is a known pilot, and the receiver finds the frame from all twelve blocks
of pilots together. A single sync word at the start would be lost whenever an over began with a
pause or under the squelch tail; the pilots still find the frame as long as enough of it was
spoken. Dropouts inside the frame are handled by the interleaver, which spreads a burst of lost
symbols thinly across the code, and by starting a fresh frame with each over.

## Prior art

- **CTCSS and DCS.** Squelch tones and digital coded squelch (a fixed 23-bit code at 134.4
  bit/s) already ride under FM voice below 300 Hz. They prove a sub-audible channel works
  through radios, but they repeat a fixed code rather than carry messages, and many repeaters
  strip or regenerate them.
- **MDC-1200 and similar signalling.** Commercial radios send a short 1200 bit/s AFSK burst
  with the radio's ID at the start or end of a transmission. It carries data but is audible
  and sits outside the speech rather than under it.
- **APRS.** Position and status in separate 1200 baud AX.25 packets, usually on their own
  frequency, never during speech.
- **Digital voice (D-STAR, DMR, System Fusion).** These carry slow data alongside voice, but the
  voice itself is digital; analog FM receivers cannot hear the QSO at all.
- **Audio watermarking.** Spread-spectrum watermarks, shaped by the audio's own spectrum so
  they stay below what listeners notice, are the closest relative of undervoice's in-band
  mode; the difference here is a channel with FM's emphasis, limiting and noise, and a
  payload decoded live rather than checked against a known mark.

## The listening test

The spec's first target is that listeners cannot reliably tell speech with data from speech
without it in a blind test. `undervoice abx` runs that test for the under-the-voice mode.
Sub-audible data sits below what radios pass to their speakers, so an ABX test of that mode
would need the flat audio of a data port.

```powershell
undervoice abx speech/*.wav --save results.csv
```

Each trial plays A and B, one with data and one without, both through the same simulated FM
link with identical noise and gain so the data is the only difference, and X, which is one of
them. Play each as often as you like (`a`, `b`, `x`), then answer `1` if X is A or `2` if X is
B; `q` stops early. Trials at every level (`--levels`, default -24, -18 and -12 dB) are
shuffled together and the level is never shown. At the end it prints, per level, how many
answers were right and the chance of doing that well by guessing; a level is "heard" when that
chance is under 5% over at least 8 answers.

- `--pause-level` keeps the data going through the pauses in the clips at that level, which
  is the test that settles how high **In pauses** can go.
- `--save results.csv` appends the answers, so several people can add to one file;
  `undervoice abx --score results.csv` reports on all of them. Keep one file per `--shape`,
  `--snr` and `--pause-level`, since the file does not record them. A file that is not a
  results file, or a folder that does not exist, is refused before the first trial.
- `--export FOLDER` writes the clips as WAVs with an answer key, to play to a listener another
  way.
- Use headphones, and your own recordings of real QSO audio if you have them; the WAVs in
  `speech/` are in the source repository, not in the pip package.

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

This is one reading of 47 CFR Part 97 as published on [eCFR](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97)
in November 2026; check the quoted sections there, since the rules change. Calling the emission
F9W, and the points drawn from 97.309(b) and 97.205(g), are interpretations, not FCC rulings.
It is not legal advice: check with ARRL or the FCC before relying on it.

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
- **Identify as usual.** 97.119 requires your callsign every 10 minutes and at the end, by CW
  or phone. Nothing in it lets the callsign in the data count as identification. undervoice can
  help: with `--cw-id` (or **CW ID before unkeying, when due** in the window), the first over
  and every over that ends 9 minutes or more after the last ID end with your callsign in Morse
  at 20 wpm before PTT drops, which 97.119(b)(1) accepts. The ID falls due a minute early
  because it can only go out as an over ends; when an over runs on past 9 minutes, the window
  reminds you to say your callsign, or to stop so the CW ID goes out. `tx --play --cw-id`
  always ends the recording with the ID. Keyed by VOX or by hand, all the time Transmit is on
  (or `tx` runs) is one over. Without CW ID, the window reminds you every 9 minutes while the
  station is in use; the command line has no reminder. Either way, still give your callsign at
  the end of the QSO.
- **Messages are ordinary traffic.** They go out as plain text in a published format, with
  both callsigns in clear, so anyone running undervoice can read them, as 97.113(a)(4) expects.
  The usual content rules apply: no business (97.113(a)(3)), and a message on behalf of someone
  who isn't a licensee is third-party traffic under 97.115.
- **Repeaters.** 97.205(g) says the control operator of a repeater that inadvertently
  retransmits something that breaks the rules is not accountable for it, but the repeater's
  licensee can still limit who and what uses it (97.205(e)). Ask first.

Still open, worth putting to ARRL: whether the FCC would treat this as phone, data or a
combined emission in practice; and whether an unscreened 32-character status needs the same
care as any other text under 97.113 (no business, no obscenity).

## Known gaps

Everything above was measured in simulation or over a sound card loopback. What only people
and radios can settle:

- **Can listeners hear it?** The blind listening test has not been run with people yet. The
  test kit ships with undervoice (`undervoice abx`, above); until results are in, the default
  level is a guess backed only by the decode margins above, and **In pauses** stays off.
- **Radio to radio and through a repeater.** Not done yet. Real radios' audio filters,
  limiters and repeaters' processing may differ from the simulator.
- **Hardware.** PTT keying (CM108, serial, rigctld), the Windows installers and the updater
  are tested in CI and against fakes of the hardware, not yet against a real interface.
- **Pauses.** With **In pauses** off, a frame spanning long pauses can be lost on a noisy
  path (see Results). That goes for message parts too.
- **Messages.** Nothing acknowledges a message, and a missed part only comes back if the sender
  presses **Send again**.

## Building from source

```powershell
git clone https://github.com/Londopy/undervoice
cd undervoice
pip install -e .[live,dev]
pytest
ruff check .
mypy undervoice
```

The Windows downloads are built by `.github/workflows/release.yml` with PyInstaller,
[Inno Setup 6](https://jrsoftware.org/isinfo.php) and [WiX v7](https://wixtoolset.org). Run it
with `dry_run` from the Actions tab (`gh workflow run release.yml -f dry_run=true`) to build
everything without publishing.

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Anything that
changes what goes on air has to update the Format section above in the same pull request.

## Speech recordings

The WAV files in `speech/` are Harvard sentences from the
[Open Speech Repository](https://www.voiptroubleshooter.com/open_speech/), used for the tests and
sweeps.

## License

MIT. See [LICENSE](LICENSE).
