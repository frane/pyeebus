"""Turn failed tests from a JUnit XML report into GitHub annotations."""

import sys
import xml.etree.ElementTree as ET

for case in ET.parse(sys.argv[1]).iter("testcase"):
    for problem in [*case.iter("failure"), *case.iter("error")]:
        text = (problem.get("message") or "") + "\n" + (problem.text or "")
        text = text.strip().replace("%", "%25").replace("\r", "").replace("\n", "%0A")[-3000:]
        print(f"::error title={case.get('classname')}.{case.get('name')}::{text}")
