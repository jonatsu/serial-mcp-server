# Open investigation: long command lines overwrite the mirrored terminal

Status: open; the reported failure has no confirmed cause or verified fix.

## Reported behavior

Long command lines sometimes overwrite the current row instead of continuing on the next row.\
The user reports this through the mirror while serial-mcp owns the serial port.\
Direct serial access with Minicom works correctly according to the user.

The affected mirror command is:

```bash
minicom -b 115200 -D /tmp/serial-mcp0
```

Minicom line wrapping was off by default. The user has adding carriage returns to line feeds enabled.\
The host terminal width during the failure is unknown.

The symptom disappeared after the serial device rebooted. The user changed nothing on the Minicom side.\
Enabling Minicom wrapping was suggested, but was not the change that preceded recovery.\
Reboot recovery does not establish which device-side state changed or whether the symptom will return.

The original transcript also contained commands sent by an agent through the owning connection.\
Whether concurrent agent input contributed to the display failure is unknown.\
Device identity and operational command payloads are omitted from this record.

## Working device-side terminal state

The user supplied these values from the working session after reboot, through the mirrored connection.\
They describe the serial device's terminal state, not a measurement of the host terminal window.\
No corresponding capture exists from the failing session.

```text
stty size
93 313

stty -a
speed 115200 baud; rows 93; columns 313; line = 0;
intr = ^C; quit = ^\; erase = ^?; kill = ^U; eof = ^D; eol = <undef>; eol2 = <undef>; swtch = <undef>; start = ^Q; stop = ^S; susp = ^Z; rprnt = ^R; werase = ^W; lnext = ^V; discard = ^O; min = 1; time = 0;
-parenb -parodd -cmspar cs8 hupcl -cstopb cread clocal -crtscts
-ignbrk -brkint -ignpar -parmrk -inpck -istrip -inlcr -igncr icrnl ixon ixoff -iuclc -ixany imaxbel iutf8
opost -olcuc -ocrnl onlcr -onocr -onlret -ofill -ofdel nl0 cr0 tab0 bs0 vt0 ff0
isig icanon -iexten echo echoe echok -echonl -noflsh -xcase -tostop -echoprt echoctl echoke -flusho -extproc

printf 'TERM=%s\n' "$TERM"
TERM=vt220
```

The device reports 313 columns, so the example command lines fit without reaching that width.\
Their successful display therefore does not demonstrate correct wrapping at the device's current column boundary.

## Local reproduction and its limits

Disposable pseudo-terminal experiments used the actual `MirrorSession`, local Bash, and Minicom 2.10.\
At 80 columns, Bash emitted a space and carriage return at the wrapping boundary without a line feed.\
With Minicom wrapping off, the continuation overwrote the current row. Enabling `-w` advanced to the next row.\
Enabling Minicom's add-carriage-return option did not repair the wrapping-off case.

The same stream produced identical display bytes through a direct raw pseudo-terminal and the actual mirror.\
These experiments reproduce a matching symptom, but do not explain the reported difference from direct serial access.\
They do not establish the serial device's shell implementation or terminal state during the original failure.

The earlier [mirror reliability ledger](../plans/archive/mirror-reliability-implementation-plan.md) records a separate\
byte-loss repair and limited hardware comparisons. It explicitly leaves the original rendering defect unresolved.\
That repair must not be treated as a verified fix for this report.

## Hypothesis and unresolved questions

A mismatch between the device shell's assumed width and Minicom's display width is a working hypothesis.\
Device-side terminal or line-editing state that resets on reboot is another aspect to investigate.\
Neither hypothesis is confirmed by the working-state capture.

The failing device-side width, shell width, terminal modes, and actual display control bytes remain unknown.\
The mechanism that established the working device-side size of 93 rows and 313 columns is also unknown.\
Minicom's unchanged configuration rules out changing its wrap option as the observed recovery action.

## Evidence to collect on recurrence

Before rebooting or changing settings, capture the device-side terminal state:

```bash
stty size
stty -a
printf 'TERM=%s\n' "$TERM"
```

Record the host terminal dimensions, Minicom wrapping state, and exact launch commands for both access methods.\
Compare the failing device-side state with the working baseline above.\
Determine whether another agent or script changed terminal state or sent input before the symptom appeared.

For a controlled reproduction, compare received serial bytes with mirror delivery and rendered cursor movement.\
Preserve carriage returns, line feeds, and escape sequences exactly; do not normalize the transport to mask rendering.\
Use harmless sample input and keep device identities and operational payloads out of retained captures.\
Further hardware access requires fresh permission.

Close this investigation only after evidence explains the original failure and verifies the correction.\
A matching local symptom or disappearance after reboot alone is insufficient.
