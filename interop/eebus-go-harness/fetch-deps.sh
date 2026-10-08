#!/bin/sh
# Check out the enbility Go stack at the commits pyeebus is tested against.
set -e
cd "$(dirname "$0")"
fetch() {
	rm -rf "deps/$1"
	git init -q "deps/$1"
	git -C "deps/$1" fetch -q --depth 1 "https://github.com/enbility/$1.git" "$2"
	git -C "deps/$1" checkout -q FETCH_HEAD
}
fetch eebus-go 8583642861c39a673c68f6e4a5089d338e0bffa1
fetch spine-go eb2cd4daba1302bad64aed6bda136b4c3884143f
fetch ship-go a84426bc38105a14b50dad23c0615bff0430af7b
