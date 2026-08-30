from quantsieve_providers.sec import SECEdgarProvider

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>EXAMPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>123456789</cusip>
    <value>1200</value>
    <shrsOrPrnAmt><sshPrnamt>3456</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>DFND</investmentDiscretion>
  </infoTable>
</informationTable>
"""


def test_parse_information_table() -> None:
    rows = SECEdgarProvider._parse_information_table(SAMPLE_XML)

    assert rows == [
        {
            "issuer": "EXAMPLE INC",
            "class": "COM",
            "cusip": "123456789",
            "value_usd": 1_200_000,
            "shares": 3456,
            "share_type": "SH",
            "investment_discretion": "DFND",
        }
    ]
