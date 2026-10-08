module eebusharness

go 1.24.1

require (
	github.com/enbility/eebus-go v0.0.0
	github.com/enbility/ship-go v0.0.0
	github.com/enbility/spine-go v0.0.0
)

// fetch-deps.sh checks out the exact commits the tests were written against
replace github.com/enbility/eebus-go => ./deps/eebus-go

replace github.com/enbility/spine-go => ./deps/spine-go

replace github.com/enbility/ship-go => ./deps/ship-go
