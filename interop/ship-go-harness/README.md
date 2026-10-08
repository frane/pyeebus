# ship-go interop harness

A minimal [ship-go](https://github.com/enbility/ship-go) hub used to test pyeebus against the reference implementation. It trusts one remote SKI, can dial a remote node, and echoes every SPINE payload back.

```bash
cd interop/ship-go-harness
go get github.com/enbility/ship-go@a84426bc38105a14b50dad23c0615bff0430af7b
go build -o harness .
SHIP_GO_HARNESS=$PWD/harness pytest ../../tests/test_interop_ship_go.py
```
