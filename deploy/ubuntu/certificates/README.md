# Russian Trusted CA bundle for Ubuntu

These public CA certificates are the complete contents of the two Linux archives linked by the
T-Bank PC/Linux certificate guide on 2026-08-18. They are committed so deployment does not have
to download trust anchors at install time.

Official sources:

- guide: https://www.tbank.ru/bank/help/certificates/
- API TLS guide: https://developer.tbank.ru/docs/tls-settings
- root archive: https://gu-st.ru/content/lending/linux_russian_trusted_root_ca_pem.zip
- subordinate archive: https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.zip

Downloaded archive SHA-256:

- root ZIP: `ca99ca9b0022ec8b99d5822502cf3f38d4797bdd02cc098996778421d72d7e24`
- subordinate ZIP: `35d8ce3ed079b1cd3a1650bf2ed2d873eee288799924dbbe128c172b65d3594e`

Certificate SHA-256 fingerprints (DER certificate fingerprint, not PEM file hash):

| File | Subject | Valid until | Certificate SHA-256 |
| --- | --- | --- | --- |
| `russian_trusted_root_ca.crt` | Russian Trusted Root CA | 2032-02-27 | `d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31` |
| `russian_trusted_root_ca_gost_2025.crt` | Минцифры России НУЦ корневой | 2040-05-24 | `5b51db721b7c34958ed7432ae917a91297dd37508b2cae4f858ffbac6bc525ef` |
| `russian_trusted_sub_ca.crt` | Russian Trusted Sub CA | 2027-03-06 | `bbbde2103e790b999ec62bd03cf625a5a2e7c316e10afe6a490eedead8b3fd9b` |
| `russian_trusted_sub_ca_2024.crt` | Russian Trusted Sub CA | 2029-07-19 | `2155785036c900dbb5f1bb2a1569c80c55595bd6bf94867a29bbddbc7d88a3f2` |
| `russian_trusted_sub_ca_gost_2025.crt` | Минцифры России НУЦ подчиненный | 2030-05-27 | `b809281bf07b865bcdd7f5746bf1ebb7ccee093d5c63b016dd91ee3b22cda8d1` |

The two GOST files in the source ZIP were DER despite the `_pem.crt` filename. They were converted
losslessly to PEM; their DER certificate fingerprints above are unchanged. `SHA256SUMS` protects
the exact committed PEM files and is checked before every install or update.

Do not replace these files from an unverified mirror. A CA update must repeat source, subject,
issuer, validity, fingerprint and deployment tests.
