# eebus-go interop harness

Runs an [eebus-go](https://github.com/enbility/eebus-go) CEM (EVSECC, EVCC, EVCEM, OPEV,
OSCEV), a spine-go based EVSE with a pluggable EV, an eebus-go controllable system (LPC, MPC)
or an eebus-go Energy Guard (LPC, LPP, MPC monitoring), so the tests can check pyeebus' SPINE
layer and use cases against the Go reference in both directions.

```bash
./fetch-deps.sh
go mod tidy
go build -o harness .
EEBUS_GO_HARNESS=$PWD/harness pytest ../../tests/test_interop_eebus_go.py
```

`-mode cem|evse|lpc|eg -port N -trust <SKI>`; in EVSE mode, stdin takes `plug`, `unplug`,
`measure <A> <Wh>` and `quit`; in LPC mode `power <W>`; in Energy Guard mode `limit <W> [s]`,
`release`, `failsafe <W>`, `failsafeduration <s>`, `heartbeat stop|start` and `nominal`, for LPP `plimit <W> [s]` (the wire value, <= 0 by LPP's sign convention), `prelease`, `pfailsafe <W>`
and `pnominal`.

Without a local Go toolchain, build with Docker (for Windows add `-e GOOS=windows` and
`-o harness.exe`):

```bash
docker run --rm -v "$PWD:/src" -w /src golang:1.24 sh -c "go mod tidy && go build -o harness ."
```
