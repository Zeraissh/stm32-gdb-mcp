from mcp_server.svd_parser import SVDParser


SVD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<device>
  <peripherals>
    <peripheral>
      <name>GPIOA</name>
      <baseAddress>0x40020000</baseAddress>
      <registers>
        <register>
          <name>MODER</name>
          <description>GPIO port mode register</description>
          <addressOffset>0x00</addressOffset>
          <size>32</size>
          <fields>
            <field>
              <name>MODER0</name>
              <bitOffset>0</bitOffset>
              <bitWidth>2</bitWidth>
              <enumeratedValues>
                <enumeratedValue>
                  <name>Input</name>
                  <value>0</value>
                </enumeratedValue>
                <enumeratedValue>
                  <name>Output</name>
                  <value>1</value>
                </enumeratedValue>
                <enumeratedValue>
                  <name>Alternate</name>
                  <value>2</value>
                </enumeratedValue>
              </enumeratedValues>
            </field>
            <field>
              <name>MODER1</name>
              <bitOffset>2</bitOffset>
              <bitWidth>2</bitWidth>
            </field>
          </fields>
        </register>
        <register derivedFrom="MODER">
          <name>OTYPER</name>
          <addressOffset>0x04</addressOffset>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""


def test_decode_register_value_returns_named_fields_and_enums(tmp_path):
    svd_path = tmp_path / "stm32.svd"
    svd_path.write_text(SVD_XML, encoding="utf-8")
    parser = SVDParser()
    parser.load(str(svd_path))

    decoded = parser.decode_register_value("GPIOA", "MODER", 0b1001)

    assert decoded["address"] == "0x40020000"
    assert decoded["value"] == "0x00000009"
    assert decoded["fields"][0]["name"] == "MODER0"
    assert decoded["fields"][0]["raw"] == 1
    assert decoded["fields"][0]["meaning"] == "Output"
    assert decoded["fields"][1]["name"] == "MODER1"
    assert decoded["fields"][1]["raw"] == 2


def test_derived_register_reuses_fields_but_has_own_address(tmp_path):
    svd_path = tmp_path / "stm32.svd"
    svd_path.write_text(SVD_XML, encoding="utf-8")
    parser = SVDParser()
    parser.load(str(svd_path))

    decoded = parser.decode_register_value("GPIOA", "OTYPER", 0b10)

    assert decoded["address"] == "0x40020004"
    assert [field["name"] for field in decoded["fields"]] == ["MODER0", "MODER1"]
