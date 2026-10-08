# eebus-go interop harness

Runs an [eebus-go](https://github.com/enbility/eebus-go) CEM (EVSECC, EVCC, EVCEM, OPEV,
OSCEV) or a spine-go based EVSE with a pluggable EV, so the tests can check pyeebus' SPINE
layer and use cases against the Go reference in both directions.

```bash
./fetch-deps.sh
go mod tidy
go build -o harness .
EEBUS_GO_HARNESS=$PWD/harness pytest ../../tests/test_interop_eebus_go.py
```

`-mode cem|evse -port N -trust <SKI>`; in EVSE mode, stdin takes `plug`, `unplug`,
`measure <A> <Wh>` and `quit`.
