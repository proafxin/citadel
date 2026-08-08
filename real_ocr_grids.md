# Real OCR raw grids (captured this session via live PaddleOCR-VL)

## Pytheas paper results table

Source: `p2075-christodoulakis.pdf` page 9

Raw OTSL:
```
<fcel>Class<fcel>Pytheas<lcel><fcel>TIRS<lcel><fcel>SP_CRF<lcel><fcel>Heuristics<lcel><nl><ucel><fcel>P<fcel>R<fcel>P<fcel>R<fcel>P<fcel>R<fcel>P<fcel>R<nl><fcel>DATA<fcel>\(99.93\pm0.02\)<fcel>\(99.90\pm0.02\)<fcel>\(99.92\pm0.01\)<fcel>\(91.94\pm0.59\)<fcel>\(99.27\pm0.11\)<fcel>\(99.80\pm0.09\)<fcel>\(99.03\pm0.13\)<fcel>\(99.89\pm0.01\)<nl><fcel>↳(top only)<fcel>\(97.37\pm0.36\)<fcel>\(97.18\pm0.76\)<fcel>\(93.72\pm0.64\)<fcel>\(85.01\pm1.52\)<fcel>\(86.89\pm1.40\)<fcel>\(84.91\pm2.09\)<fcel>\(67.94\pm1.78\)<fcel>\(80.99\pm1.55\)<nl><fcel>↳(bottom only)<fcel>\(98.68\pm0.33\)<fcel>\(98.48\pm0.56\)<fcel>\(91.88\pm0.65\)<fcel>\(83.31\pm1.30\)<fcel>\(95.99\pm0.58\)<fcel>\(93.71\pm1.39\)<fcel>\(80.11\pm1.60\)<fcel>\(95.52\pm1.12\)<nl><fcel>HEADER<fcel>\(95.13\pm0.94\)<fcel>\(98.09\pm0.56\)<fcel>\(86.29\pm3.02\)<fcel>\(85.76\pm1.28\)<fcel>\(88.21\pm1.98\)<fcel>\(82.04\pm1.67\)<fcel>\(74.15\pm1.87\)<fcel>\(82.63\pm1.86\)<nl><fcel>SUBHEADER<fcel>\(88.14\pm3.34\)<fcel>\(88.72\pm1.98\)<fcel>-<fcel>-<fcel>\(50.00\pm16.67\)<fcel>\(0.0\pm0.0\)<fcel>-<fcel>-<nl><fcel>CONTEXT<fcel>\(82.94\pm4.03\)<fcel>\(92.81\pm1.99\)<fcel>\(5.98\pm1.00\)<fcel>\(74.35\pm4.19\)<fcel>\(67.01\pm6.28\)<fcel>\(50.14\pm4.22\)<fcel>-<fcel>-<nl><fcel>FOOTNOTE<fcel>\(91.64\pm6.51\)<fcel>\(88.68\pm3.76\)<fcel>-<fcel>-<fcel>\(76.07\pm9.34\)<fcel>\(47.29\pm5.85\)<fcel>-<fcel>-<nl><fcel>OTHER<fcel>\(90.0\pm10.0\)<fcel>\(60.08\pm16.30\)<fcel>\(0.30\pm0.18\)<fcel>\(80.08\pm13.28\)<fcel>\(31.47\pm14.98\)<fcel>\(55.00\pm15.29\)<fcel>\(90.0\pm10.0\)<fcel>\(50.08\pm16.64\)<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: Class | Pytheas |  | TIRS |  | SP_CRF |  | Heuristics | 
1:  | P | R | P | R | P | R | P | R
2: DATA | \(99.93\pm0.02\) | \(99.90\pm0.02\) | \(99.92\pm0.01\) | \(91.94\pm0.59\) | \(99.27\pm0.11\) | \(99.80\pm0.09\) | \(99.03\pm0.13\) | \(99.89\pm0.01\)
3: ↳(top only) | \(97.37\pm0.36\) | \(97.18\pm0.76\) | \(93.72\pm0.64\) | \(85.01\pm1.52\) | \(86.89\pm1.40\) | \(84.91\pm2.09\) | \(67.94\pm1.78\) | \(80.99\pm1.55\)
4: ↳(bottom only) | \(98.68\pm0.33\) | \(98.48\pm0.56\) | \(91.88\pm0.65\) | \(83.31\pm1.30\) | \(95.99\pm0.58\) | \(93.71\pm1.39\) | \(80.11\pm1.60\) | \(95.52\pm1.12\)
5: HEADER | \(95.13\pm0.94\) | \(98.09\pm0.56\) | \(86.29\pm3.02\) | \(85.76\pm1.28\) | \(88.21\pm1.98\) | \(82.04\pm1.67\) | \(74.15\pm1.87\) | \(82.63\pm1.86\)
6: SUBHEADER | \(88.14\pm3.34\) | \(88.72\pm1.98\) | - | - | \(50.00\pm16.67\) | \(0.0\pm0.0\) | - | -
7: CONTEXT | \(82.94\pm4.03\) | \(92.81\pm1.99\) | \(5.98\pm1.00\) | \(74.35\pm4.19\) | \(67.01\pm6.28\) | \(50.14\pm4.22\) | - | -
8: FOOTNOTE | \(91.64\pm6.51\) | \(88.68\pm3.76\) | - | - | \(76.07\pm9.34\) | \(47.29\pm5.85\) | - | -
9: OTHER | \(90.0\pm10.0\) | \(60.08\pm16.30\) | \(0.30\pm0.18\) | \(80.08\pm13.28\) | \(31.47\pm14.98\) | \(55.00\pm15.29\) | \(90.0\pm10.0\) | \(50.08\pm16.64\)
```

---

## Malaysian gazette CODE list (High Court)

Source: `135. Arahan Amalan Bil 5 Tahun 1988.pdf` page 4

Raw OTSL:
```
<fcel>CODE<fcel>HIGH COURT<nl><fcel>11<fcel>Civil Appeal from Magistrates' Courts<nl><fcel>12<fcel>Civil Appeal from Sessions Courts<nl><fcel>13<fcel>Civil Revision<nl><fcel>14<fcel>Tax Appeal<nl><fcel>15<fcel>Land Reference<nl><fcel>16<fcel>Appeal from Administrative Tribunals<nl><fcel>17<fcel>Legal Profession Act, 1976<nl><fcel>18<fcel>Petition for Admission & Enrolment as Advocate & Solicitor<nl><fcel>21<fcel>Civil Proceedings by & against the Government<nl><fcel>22<fcel>Civil Suit - General<nl><fcel>23<fcel>Civil Suit - Tort<nl><fcel>24<fcel>Originating Summons<nl><fcel>25<fcel>Originating Motion<nl><fcel>26<fcel>Originating Petition<nl><fcel>27<fcel>Admiralty<nl><fcel>28<fcel>Companies Winding-Up<nl><fcel>29<fcel>Bankruptcy<nl><fcel>31<fcel>Letters of Administration<nl><fcel>32<fcel>Probate<nl><fcel>33<fcel>Divorce<nl><fcel>34<fcel>Adoption<nl><fcel>35<fcel>Distress Suit<nl><fcel>36<fcel>Application for Execution - Prohibitory Order<nl><fcel>37<fcel>Application for Execution - Movable Property<nl><fcel>38<fcel>Application for Execution - Immovable Property<nl><fcel>41<fcel>Criminal Appeal from Magistrates' Courts<nl><fcel>42<fcel>Criminal Appeal from Sessions Courts<nl><fcel>43<fcel>Criminal Revision<nl><fcel>44<fcel>Criminal Application<nl><fcel>45<fcel>Criminal Jury Trial<nl><fcel>46<fcel>Criminal Assessor Trial<nl><fcel>47<fcel>Criminal Trial by single Judge<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: CODE | HIGH COURT
1: 11 | Civil Appeal from Magistrates' Courts
2: 12 | Civil Appeal from Sessions Courts
3: 13 | Civil Revision
4: 14 | Tax Appeal
5: 15 | Land Reference
6: 16 | Appeal from Administrative Tribunals
7: 17 | Legal Profession Act, 1976
8: 18 | Petition for Admission & Enrolment as Advocate & Solicitor
9: 21 | Civil Proceedings by & against the Government
10: 22 | Civil Suit - General
11: 23 | Civil Suit - Tort
12: 24 | Originating Summons
13: 25 | Originating Motion
14: 26 | Originating Petition
15: 27 | Admiralty
16: 28 | Companies Winding-Up
17: 29 | Bankruptcy
18: 31 | Letters of Administration
19: 32 | Probate
20: 33 | Divorce
21: 34 | Adoption
22: 35 | Distress Suit
23: 36 | Application for Execution - Prohibitory Order
24: 37 | Application for Execution - Movable Property
25: 38 | Application for Execution - Immovable Property
26: 41 | Criminal Appeal from Magistrates' Courts
27: 42 | Criminal Appeal from Sessions Courts
28: 43 | Criminal Revision
29: 44 | Criminal Application
30: 45 | Criminal Jury Trial
31: 46 | Criminal Assessor Trial
32: 47 | Criminal Trial by single Judge
```

---

## Malaysian gazette CODE list (Sessions Court)

Source: `135. Arahan Amalan Bil 5 Tahun 1988.pdf` page 5

Raw OTSL:
```
<fcel>CODE<fcel>SESSIONS COURTS<nl><fcel>51<fcel>Civil Proceedings by & against the Government<nl><fcel>52<fcel>Civil Summons - General<nl><fcel>53<fcel>Civil Summons - Tort<nl><fcel>54<fcel>Originating Application<nl><fcel>55<fcel>Originating Petition<nl><fcel>56<fcel>Application for Execution<nl><fcel>61<fcel>Criminal Arrest Case against Public Servants<nl><fcel>62<fcel>Criminal Arrest Case - General<nl><fcel>63<fcel>Criminal Summons Case<nl><fcel>64<fcel>Criminal Application<nl><fcel>71<fcel>FIRST CLASS MAGISTRATES' COURTS<nl><fcel>72<fcel>Civil Proceedings by & against the Government<nl><fcel>73<fcel>Civil Summons - General<nl><fcel>74<fcel>Civil Summons - Tort<nl><fcel>75<fcel>Originating Application<nl><fcel>76<fcel>Originating Petition<nl><ecel><fcel>Application for Execution<nl><fcel>81<fcel>Committal Case to the High Court<nl><fcel>82<fcel>Criminal Arrest Case against Public Servants<nl><fcel>83<fcel>Criminal Arrest Case - General<nl><fcel>84<fcel>Juvenile Case<nl><fcel>85<fcel>Criminal Summons Case - General<nl><fcel>86<fcel>Traffic Case<nl><fcel>87<fcel>Departmental Summons Case<nl><fcel>88<fcel>Death Inquiry<nl><fcel>89<fcel>Criminal Application<nl><fcel>SECOND CLASS MAGISTRATES' COURTS<lcel><nl><fcel>91<fcel>Civil Summons by Government<nl><fcel>92<fcel>Civil Summons by Individual<nl><fcel>93<fcel>Civil Summons by Firm or Society<nl><fcel>94<fcel>Criminal Arrest Case<nl><fcel>95<fcel>Criminal Summons Case - General<nl><fcel>96<fcel>Traffic Case<nl><fcel>97<fcel>Departmental Summons Case<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: CODE | SESSIONS COURTS
1: 51 | Civil Proceedings by & against the Government
2: 52 | Civil Summons - General
3: 53 | Civil Summons - Tort
4: 54 | Originating Application
5: 55 | Originating Petition
6: 56 | Application for Execution
7: 61 | Criminal Arrest Case against Public Servants
8: 62 | Criminal Arrest Case - General
9: 63 | Criminal Summons Case
10: 64 | Criminal Application
11: 71 | FIRST CLASS MAGISTRATES' COURTS
12: 72 | Civil Proceedings by & against the Government
13: 73 | Civil Summons - General
14: 74 | Civil Summons - Tort
15: 75 | Originating Application
16: 76 | Originating Petition
17:  | Application for Execution
18: 81 | Committal Case to the High Court
19: 82 | Criminal Arrest Case against Public Servants
20: 83 | Criminal Arrest Case - General
21: 84 | Juvenile Case
22: 85 | Criminal Summons Case - General
23: 86 | Traffic Case
24: 87 | Departmental Summons Case
25: 88 | Death Inquiry
26: 89 | Criminal Application
27: SECOND CLASS MAGISTRATES' COURTS | 
28: 91 | Civil Summons by Government
29: 92 | Civil Summons by Individual
30: 93 | Civil Summons by Firm or Society
31: 94 | Criminal Arrest Case
32: 95 | Criminal Summons Case - General
33: 96 | Traffic Case
34: 97 | Departmental Summons Case
```

---

## Kazakh doc repeated page-header banner (p1)

Source: `Draft_Agreement_5ezbECB.pdf` page 1

Raw OTSL:
```
<fcel>astana hub<fcel>Internal regulatory document\nAstana Hub International Technopark of IT startups Corporate Fund<fcel>Page 1<nl><fcel>REGISTRATION PROCEDURE FOR PARTICIPANTS OF ASTANA HUB INTERNATIONAL TECHNOLOGY PARK OF IT STARTUPS<lcel><lcel><nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: astana hub | Internal regulatory document\nAstana Hub International Technopark of IT startups Corporate Fund | Page 1
1: REGISTRATION PROCEDURE FOR PARTICIPANTS OF ASTANA HUB INTERNATIONAL TECHNOLOGY PARK OF IT STARTUPS |  | 
```

---

## Kazakh doc repeated page-header banner (p3)

Source: `Draft_Agreement_5ezbECB.pdf` page 3

Raw OTSL:
```
<fcel>astana hub<fcel>Internal regulatory document\nAstana Hub International Technopark of IT startups Corporate Fund<fcel>Page 2<nl><fcel>REGISTRATION PROCEDURE FOR PARTICIPANTS OF ASTANA HUB INTERNATIONAL TECHNOLOGY PARK OF IT STARTUPS<lcel><lcel><nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: astana hub | Internal regulatory document\nAstana Hub International Technopark of IT startups Corporate Fund | Page 2
1: REGISTRATION PROCEDURE FOR PARTICIPANTS OF ASTANA HUB INTERNATIONAL TECHNOLOGY PARK OF IT STARTUPS |  | 
```

---

## Kazakh legal claim form (colon pattern) p4

Source: `defence.pdf` page 4

Raw OTSL:
```
<fcel>No. Tuntutan<fcel>:<fcel>TTPM-WP-(P)-1172-2026<nl><fcel>Tarikh Pendengaran<fcel>:<ecel><nl><fcel>Masa Pendengaran<fcel>:<ecel><nl><fcel>No. Resit Borang 1<fcel>:<ecel><nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: No. Tuntutan | : | TTPM-WP-(P)-1172-2026
1: Tarikh Pendengaran | : | 
2: Masa Pendengaran | : | 
3: No. Resit Borang 1 | : | 
```

---

## Kazakh legal claim form (colon pattern) p6a

Source: `defence.pdf` page 6

Raw OTSL:
```
<fcel>Nama Pihak Yang Menuntut<fcel>:<fcel>MASUM BILLAL<nl><fcel>No. Kad Pengenalan/Pasport<fcel>:<fcel>B00580030<nl><fcel>Alamat Surat Menyurat<fcel>:<fcel>A-12-07-S1, 18, Jalan Dewan Sultan Ismail, The Luxe Colony by Infinitum, Kampung Baru, KLCC, KL 50300 Kuala Lumpur Kuala Lumpur<nl><fcel>No. Telefon<fcel>:<fcel>01115631120<nl><fcel>No. Faks/E-mel<fcel>:<fcel>/ billalmasum93@gmail.com<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: Nama Pihak Yang Menuntut | : | MASUM BILLAL
1: No. Kad Pengenalan/Pasport | : | B00580030
2: Alamat Surat Menyurat | : | A-12-07-S1, 18, Jalan Dewan Sultan Ismail, The Luxe Colony by Infinitum, Kampung Baru, KLCC, KL 50300 Kuala Lumpur Kuala Lumpur
3: No. Telefon | : | 01115631120
4: No. Faks/E-mel | : | / billalmasum93@gmail.com
```

---

## Kazakh legal claim form (colon pattern) p6b

Source: `defence.pdf` page 6

Raw OTSL:
```
<fcel>Nama Penentang/Syarikat/ Pertubuhan Perbadanan/ Pertubuhan/Firma<fcel>:<fcel>ICONIX CO-LIVING SDN BHD<nl><ecel><fcel>1439156-A<ecel><nl><fcel>No. Kad Pengenalan/<fcel>:<ecel><nl><fcel>No. Pendaftaran Syarikat/ Pertubuhan Perbadanan/ Pertubuhan/Firma<ecel><ecel><nl><fcel>Alamat Surat Menyurat<fcel>:<fcel>UNIT NO 20-01, MERCU ASPIRE, KL ECO CITY<nl><ecel><fcel>:<fcel>59200 Kuala Lumpur<nl><ecel><fcel>/ admin@iconixpropertymgmt.com<ecel><nl><fcel>No. Telefon<fcel>:<ecel><nl><fcel>No. Faks/E-mel<fcel>:<ecel><nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: Nama Penentang/Syarikat/ Pertubuhan Perbadanan/ Pertubuhan/Firma | : | ICONIX CO-LIVING SDN BHD
1:  | 1439156-A | 
2: No. Kad Pengenalan/ | : | 
3: No. Pendaftaran Syarikat/ Pertubuhan Perbadanan/ Pertubuhan/Firma |  | 
4: Alamat Surat Menyurat | : | UNIT NO 20-01, MERCU ASPIRE, KL ECO CITY
5:  | : | 59200 Kuala Lumpur
6:  | / admin@iconixpropertymgmt.com | 
7: No. Telefon | : | 
8: No. Faks/E-mel | : | 
```

---

## Retail invoice line items

Source: `INVOICE 864067.pdf` page 1

Raw OTSL:
```
<fcel>DATE / TIME<lcel><fcel>PAGE<fcel>CASH SALE NO.<lcel><fcel>USER<fcel>SALESMAN<nl><fcel>22/06/2026 13:14<lcel><fcel>1 of 1<fcel>LY-G026-864067<lcel><fcel>Yusri<ecel><nl><fcel>NO<fcel>ITEM DESCRIPTION<lcel><lcel><fcel>QTY<fcel>UNIT PRICE<fcel>AMOUNT (RM)<nl><fcel>1<fcel>NB-MSI-V16HX-AI-A2XWJG-478MY-GRY VECTOR 16HX AI / 16 QHD+ IPS 240HZ / U9-275HX / 16GB D5 / 1TB SSD / NVIDIA RTX5090 24GB D7 / W11 / 2 YR + 1ST YR ITW / GAMING BP / COSMOS GRAY / 9S7-15M352-478 SN#:K2507N0044577<lcel><lcel><fcel>1<fcel>14,500.00<fcel>14,500.00<nl><ucel><ucel><xcel><xcel><fcel>1<fcel>14,500.00<ecel><nl><ucel><ucel><xcel><xcel><fcel>TOTAL<fcel>14,500.00<ecel><nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: DATE / TIME |  | PAGE | CASH SALE NO. |  | USER | SALESMAN
1: 22/06/2026 13:14 |  | 1 of 1 | LY-G026-864067 |  | Yusri | 
2: NO | ITEM DESCRIPTION |  |  | QTY | UNIT PRICE | AMOUNT (RM)
3: 1 | NB-MSI-V16HX-AI-A2XWJG-478MY-GRY VECTOR 16HX AI / 16 QHD+ IPS 240HZ / U9-275HX / 16GB D5 / 1TB SSD / NVIDIA RTX5090 24GB D7 / W11 / 2 YR + 1ST YR ITW / GAMING BP / COSMOS GRAY / 9S7-15M352-478 SN#:K2507N0044577 |  |  | 1 | 14,500.00 | 14,500.00
4:  |  |  |  | 1 | 14,500.00 | 
5:  |  |  |  | TOTAL | 14,500.00 | 
```

---

## Degree-plan transcript block A

Source: `SAA_STD_DS.pdf` page 1

Raw OTSL:
```
<fcel>A. ENG 101<lcel><lcel><lcel><lcel><lcel><lcel><nl><fcel>2026 Sum<fcel>ENG<fcel>101<fcel>College Composition<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>1B. ENG 121<lcel><lcel><lcel><lcel><lcel><lcel><nl><fcel>2026 Sum<fcel>ENG<fcel>121<fcel>College Composition II<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>1C. POS 101, PCJ 215, or COM 210<lcel><lcel><lcel><lcel><lcel><lcel><nl><fcel>2026 Sum<fcel>POS<fcel>101<fcel>American Government<ecel><fcel>3.00<fcel>In Progress<nl><fcel>1D. ENG 101<lcel><lcel><lcel><lcel><lcel><lcel><nl><fcel>2026 Sum<fcel>ENG<fcel>101<fcel>College Composition<fcel>T<fcel>3.00<fcel>Transfer<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: A. ENG 101 |  |  |  |  |  | 
1: 2026 Sum | ENG | 101 | College Composition | T | 3.00 | Transfer
2: 1B. ENG 121 |  |  |  |  |  | 
3: 2026 Sum | ENG | 121 | College Composition II | T | 3.00 | Transfer
4: 1C. POS 101, PCJ 215, or COM 210 |  |  |  |  |  | 
5: 2026 Sum | POS | 101 | American Government |  | 3.00 | In Progress
6: 1D. ENG 101 |  |  |  |  |  | 
7: 2026 Sum | ENG | 101 | College Composition | T | 3.00 | Transfer
```

---

## Degree-plan transcript block B

Source: `SAA_STD_DS.pdf` page 2

Raw OTSL:
```
<fcel>2026 Sum<fcel>SOC<fcel>100<fcel>Introduction to Sociology<ecel><fcel>3.00<fcel>In Progress<nl><fcel>5B. HTY 115, HTY 116, HTY 161, HTY 162<lcel><lcel><lcel><lcel><lcel><lcel><nl><fcel>2026 Sum<fcel>HTY<fcel>115<fcel>World Civilization I<ecel><fcel>3.00<fcel>In Progress<nl><fcel>5C. FRE 101 or SPA 101<lcel><lcel><lcel><lcel><lcel><lcel><nl><fcel>2026 Sum<fcel>SPA<fcel>101<fcel>Elementary Spanish I<ecel><fcel>3.00<fcel>In Progress<nl><fcel>5D. PHI 151, PHI 152 OR POS 211 OR SWK 202<lcel><lcel><lcel><lcel><lcel><lcel><nl><fcel>2026 Sum<fcel>PHI<fcel>152<fcel>Introduction to Ethics<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>5E. POS 101, POS 211, OR POS 332<lcel><lcel><lcel><lcel><lcel><lcel><nl><fcel>2026 Sum<fcel>POS<fcel>101<fcel>American Government<ecel><fcel>3.00<fcel>In Progress<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: 2026 Sum | SOC | 100 | Introduction to Sociology |  | 3.00 | In Progress
1: 5B. HTY 115, HTY 116, HTY 161, HTY 162 |  |  |  |  |  | 
2: 2026 Sum | HTY | 115 | World Civilization I |  | 3.00 | In Progress
3: 5C. FRE 101 or SPA 101 |  |  |  |  |  | 
4: 2026 Sum | SPA | 101 | Elementary Spanish I |  | 3.00 | In Progress
5: 5D. PHI 151, PHI 152 OR POS 211 OR SWK 202 |  |  |  |  |  | 
6: 2026 Sum | PHI | 152 | Introduction to Ethics | T | 3.00 | Transfer
7: 5E. POS 101, POS 211, OR POS 332 |  |  |  |  |  | 
8: 2026 Sum | POS | 101 | American Government |  | 3.00 | In Progress
```

---

## Degree-plan transcript block C

Source: `SAA_STD_DS.pdf` page 4

Raw OTSL:
```
<fcel>2026 Sum<fcel>COS<fcel>3XX<fcel>Computer Science Elective<fcel>T<fcel>0.67<fcel>Transfer<nl><fcel>2026 Sum<fcel>COS<fcel>3XX<fcel>Computer Science Elective<fcel>T<fcel>0.67<fcel>Transfer<nl><fcel>2026 Sum<fcel>COS<fcel>3XX<fcel>Computer Science Elective<fcel>T<fcel>0.67<fcel>Transfer<nl><fcel>2026 Sum<fcel>COS<fcel>3XX<fcel>Computer Science Elective<fcel>T<fcel>0.67<fcel>Transfer<nl><fcel>2026 Sum<fcel>COS<fcel>3XX<fcel>Computer Science Elective<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>2026 Sum<fcel>COS<fcel>4XX<fcel>Computer Science Elective<fcel>T<fcel>4.00<fcel>Transfer<nl><fcel>2026 Sum<fcel>COS<fcel>4XX<fcel>Computer Science Elective<fcel>T<fcel>4.00<fcel>Transfer<nl><fcel>2026 Sum<fcel>ENG<fcel>101<fcel>College Composition<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>2026 Sum<fcel>ENG<fcel>121<fcel>College Composition II<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>2026 Sum<fcel>ENG<fcel>211<fcel>Intro. to Creative Writing<ecel><fcel>3.00<fcel>In Progress<nl><fcel>2026 Sum<fcel>ENV<fcel>110<fcel>Intro to Environmental Science<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>2026 Sum<fcel>HTY<fcel>115<fcel>World Civilization I<ecel><fcel>3.00<fcel>In Progress<nl><fcel>2026 Sum<fcel>MAT<fcel>3XX<fcel>Math Elective<fcel>T<fcel>0.67<fcel>Transfer<nl><fcel>2026 Sum<fcel>MAT<fcel>3XX<fcel>Math Elective<fcel>T<fcel>0.67<fcel>Transfer<nl><fcel>2026 Sum<fcel>MAT<fcel>117<fcel>College Algebra<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>2026 Sum<fcel>MAT<fcel>201<fcel>Probability & Statistics I<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>2026 Sum<fcel>MAT<fcel>253<fcel>Discrete Mathematics<fcel>T<fcel>3.33<fcel>Transfer<nl><fcel>2026 Sum<fcel>PHI<fcel>152<fcel>Introduction to Ethics<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>2026 Sum<fcel>POS<fcel>101<fcel>American Government<ecel><fcel>3.00<fcel>In Progress<nl><fcel>2026 Sum<fcel>PSY<fcel>100<fcel>General Psychology<fcel>T<fcel>3.00<fcel>Transfer<nl><fcel>2026 Sum<fcel>SOC<fcel>100<fcel>Introduction to Sociology<ecel><fcel>3.00<fcel>In Progress<nl><fcel>2026 Sum<fcel>SPA<fcel>101<fcel>Elementary Spanish I<ecel><fcel>3.00<fcel>In Progress<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: 2026 Sum | COS | 3XX | Computer Science Elective | T | 0.67 | Transfer
1: 2026 Sum | COS | 3XX | Computer Science Elective | T | 0.67 | Transfer
2: 2026 Sum | COS | 3XX | Computer Science Elective | T | 0.67 | Transfer
3: 2026 Sum | COS | 3XX | Computer Science Elective | T | 0.67 | Transfer
4: 2026 Sum | COS | 3XX | Computer Science Elective | T | 3.00 | Transfer
5: 2026 Sum | COS | 4XX | Computer Science Elective | T | 4.00 | Transfer
6: 2026 Sum | COS | 4XX | Computer Science Elective | T | 4.00 | Transfer
7: 2026 Sum | ENG | 101 | College Composition | T | 3.00 | Transfer
8: 2026 Sum | ENG | 121 | College Composition II | T | 3.00 | Transfer
9: 2026 Sum | ENG | 211 | Intro. to Creative Writing |  | 3.00 | In Progress
10: 2026 Sum | ENV | 110 | Intro to Environmental Science | T | 3.00 | Transfer
11: 2026 Sum | HTY | 115 | World Civilization I |  | 3.00 | In Progress
12: 2026 Sum | MAT | 3XX | Math Elective | T | 0.67 | Transfer
13: 2026 Sum | MAT | 3XX | Math Elective | T | 0.67 | Transfer
14: 2026 Sum | MAT | 117 | College Algebra | T | 3.00 | Transfer
15: 2026 Sum | MAT | 201 | Probability & Statistics I | T | 3.00 | Transfer
16: 2026 Sum | MAT | 253 | Discrete Mathematics | T | 3.33 | Transfer
17: 2026 Sum | PHI | 152 | Introduction to Ethics | T | 3.00 | Transfer
18: 2026 Sum | POS | 101 | American Government |  | 3.00 | In Progress
19: 2026 Sum | PSY | 100 | General Psychology | T | 3.00 | Transfer
20: 2026 Sum | SOC | 100 | Introduction to Sociology |  | 3.00 | In Progress
21: 2026 Sum | SPA | 101 | Elementary Spanish I |  | 3.00 | In Progress
```

---

## Digital signature record (Kazakh)

Source: `Digital nomad certificate.pdf` page 2

Raw OTSL:
```
<fcel>№<fcel>ЭЦК иесі<fcel>Цифрлык колтанба туралы мәліметтер<fcel>Кол койылғаһан күні мен уакыты<nl><fcel>1<fcel>БАЙТУРСЫНОВ ФИЗЗАТ<fcel>МИIGigYJ****<fcel>02.07.2026 19:42<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: № | ЭЦК иесі | Цифрлык колтанба туралы мәліметтер | Кол койылғаһан күні мен уакыты
1: 1 | БАЙТУРСЫНОВ ФИЗЗАТ | МИIGigYJ**** | 02.07.2026 19:42
```

---

## Digital signature record + footnote (Russian)

Source: `Digital nomad certificate.pdf` page 4

Raw OTSL:
```
<fcel>№<fcel>Владелец ЭЦП<fcel>Сведения о цифровой подписи<fcel>Дата и время подписи<nl><fcel>1<fcel>БАЙТУРСЫНОВ ФИЗЗАТ<fcel>МПIGigYJ****<fcel>02.07.2026 в 19:42<nl><fcel>Данный документ согласно пункту 1 статьи 7 ЗРК от 7 января 2003 года "Об электронном документе и электронной цифровой подписи" равнозначен документу на бумажном носителе.<lcel><lcel><lcel><nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: № | Владелец ЭЦП | Сведения о цифровой подписи | Дата и время подписи
1: 1 | БАЙТУРСЫНОВ ФИЗЗАТ | МПIGigYJ**** | 02.07.2026 в 19:42
2: Данный документ согласно пункту 1 статьи 7 ЗРК от 7 января 2003 года "Об электронном документе и электронной цифровой подписи" равнозначен документу на бумажном носителе. |  |  | 
```

---

## Paired role/percent point-system table

Source: `defence.pdf` page 16

Raw OTSL:
```
<fcel>Renewal of Tenancy, based on Point system and Recommendation<lcel><lcel><lcel><lcel><nl><fcel>Community Executives & Manager (CE)<fcel>60%<ecel><fcel>Booking Team<fcel>10%<nl><fcel>Credit and Collections<fcel>10%<ecel><fcel>Other Tenants/Residence<fcel>10%<nl><fcel>Landlord<fcel>5%<ecel><fcel>JMB/ Others<fcel>5%<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: Renewal of Tenancy, based on Point system and Recommendation |  |  |  | 
1: Community Executives & Manager (CE) | 60% |  | Booking Team | 10%
2: Credit and Collections | 10% |  | Other Tenants/Residence | 10%
3: Landlord | 5% |  | JMB/ Others | 5%
```

---

## Multi-line-cell checklist table

Source: `defence.pdf` page 16

Raw OTSL:
```
<fcel>Fridge & Freezer – Leave ample space for air circulation to prevent fridge damage<fcel>Kitchen Cupboard & Drawer / Shelves – Only CLEAN Utensils<fcel>Air Cond Compressors – must be CLEAR of ALL items to prevent damage/fire.<nl><fcel>Shoe racks – Neat and Tidy<fcel>Furniture – Clean<fcel>Living/Rooms - Clean<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: Fridge & Freezer – Leave ample space for air circulation to prevent fridge damage | Kitchen Cupboard & Drawer / Shelves – Only CLEAN Utensils | Air Cond Compressors – must be CLEAR of ALL items to prevent damage/fire.
1: Shoe racks – Neat and Tidy | Furniture – Clean | Living/Rooms - Clean
```

---

## Kazakh employee-info blank form (no colon)

Source: `Draft_Agreement_5ezbECB.pdf` page 14

Raw OTSL:
```
<fcel>No.<fcel>Surname, name, patronymic (if available), including in Latin letters<fcel>Date of birth<fcel>Citizenship (country of permanent residence)<fcel>Number, date of issue and issuing authority of the passport (identity document)<fcel>Availability of a C3 visa obtained under the Technopark benefits<fcel>Expected period of stay (month, year) in the territory of the Republic of Kazakhstan<fcel>Information about qualifications<fcel>Information about visa extension<fcel>Purpose of arrival on the territory of the Republic of Kazakhstan<fcel>Address of residence in the Republic of Kazakhstan<nl><fcel>1<ecel><ecel><ecel><ecel><ecel><ecel><ecel><ecel><ecel><ecel><nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: No. | Surname, name, patronymic (if available), including in Latin letters | Date of birth | Citizenship (country of permanent residence) | Number, date of issue and issuing authority of the passport (identity document) | Availability of a C3 visa obtained under the Technopark benefits | Expected period of stay (month, year) in the territory of the Republic of Kazakhstan | Information about qualifications | Information about visa extension | Purpose of arrival on the territory of the Republic of Kazakhstan | Address of residence in the Republic of Kazakhstan
1: 1 |  |  |  |  |  |  |  |  |  | 
```

---

## Kazakh family-info blank form (no colon)

Source: `Draft_Agreement_5ezbECB.pdf` page 16

Raw OTSL:
```
<fcel>No.<fcel>Surname, name, patronymic (if any), including in Latin letters<fcel>Date of birth<fcel>Citizenship (country of permanent residence)<fcel>Number, date of issue and issuing authority of the passport (identity document)<fcel>Expected period of stay (month, year) in the territory of the Republic of Kazakhstan<fcel>Information about qualifications<fcel>Information about visa extension<fcel>Purpose of arrival on the territory of the Republic of Kazakhstan<fcel>Address of residence in the Republic of Kazakhstan<nl><fcel>1<ecel><ecel><ecel><ecel><ecel><ecel><ecel><ecel><ecel><nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: No. | Surname, name, patronymic (if any), including in Latin letters | Date of birth | Citizenship (country of permanent residence) | Number, date of issue and issuing authority of the passport (identity document) | Expected period of stay (month, year) in the territory of the Republic of Kazakhstan | Information about qualifications | Information about visa extension | Purpose of arrival on the territory of the Republic of Kazakhstan | Address of residence in the Republic of Kazakhstan
1: 1 |  |  |  |  |  |  |  |  | 
```

---

## Kazakh EDS-signature blank form (no colon)

Source: `Draft_Agreement_5ezbECB.pdf` page 16

Raw OTSL:
```
<fcel>No.<fcel>EDS Holder<fcel>Personal Identifier (IIN)<fcel>Participant Identifier (BIN)<fcel>Date and time of sig<nl><fcel>1<ecel><ecel><ecel><ecel><nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: No. | EDS Holder | Personal Identifier (IIN) | Participant Identifier (BIN) | Date and time of sig
1: 1 |  |  |  | 
```

---

## Russian address/applicant block

Source: `98630_rus_20260706 (1).pdf` page 24

Raw OTSL:
```
<fcel>город, область<nl><fcel>фамилия, имя, отчество (при его наличии) заявителя,<nl><fcel>представителя юридического лица<nl><fcel>место постоянного жительства (для юридических лиц — адрес регистрации юридического лица)<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: город, область
1: фамилия, имя, отчество (при его наличии) заявителя,
2: представителя юридического лица
3: место постоянного жительства (для юридических лиц — адрес регистрации юридического лица)
```

---

## Russian visitor-list table (numbered cols)

Source: `98630_rus_20260706 (1).pdf` page 25

Raw OTSL:
```
<fcel>№ п/п<fcel>Фамилия, имя, отчество (при его наличии) (заполняется в строгом соответствии с паспортом приглашаемого лица)<fcel>Гражданство, вид документа, номер документа, дата выдачи и срок действия<fcel>Дата рождения<fcel>Месторождения<fcel>Национальность<fcel>Пол<fcel>Страна, адрес и место постоянного жительства<fcel>Индивидуальный идентификационный номер иностранца<nl><fcel>1<fcel>2<fcel>3<fcel>4<fcel>5<fcel>6<fcel>7<fcel>8<fcel>9<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: № п/п | Фамилия, имя, отчество (при его наличии) (заполняется в строгом соответствии с паспортом приглашаемого лица) | Гражданство, вид документа, номер документа, дата выдачи и срок действия | Дата рождения | Месторождения | Национальность | Пол | Страна, адрес и место постоянного жительства | Индивидуальный идентификационный номер иностранца
1: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9
```

---

## Russian visitor-list table variant

Source: `98630_rus_20260706 (1).pdf` page 25

Raw OTSL:
```
<fcel>№ п/п<fcel>Фамилия, имя, отчество (при его наличии) (заполняется в строгом соответствии с паспор-том приглашаемого лица)<fcel>Гражданство, вид документа, номер документа, дата вы-дачи и срок дей-ствия<fcel>Дата рож-де-ния<fcel>Ме-сто рож-де-ния<fcel>Степень родства с трудо-вым им-мигран-том<fcel>На-цио-наль-ность<fcel>Пол<fcel>Стра-на, ад-рес и место посто-янного янно-го жи-тель-ства<fcel>Индиви-дуаль-ный иденти-фикаци-онный номер ино-странца<nl><fcel>1<fcel>2<fcel>3<fcel>4<fcel>5<fcel>6<fcel>7<fcel>8<fcel>9<fcel>10<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: № п/п | Фамилия, имя, отчество (при его наличии) (заполняется в строгом соответствии с паспор-том приглашаемого лица) | Гражданство, вид документа, номер документа, дата вы-дачи и срок дей-ствия | Дата рож-де-ния | Ме-сто рож-де-ния | Степень родства с трудо-вым им-мигран-том | На-цио-наль-ность | Пол | Стра-на, ад-рес и место посто-янного янно-го жи-тель-ства | Индиви-дуаль-ный иденти-фикаци-онный номер ино-странца
1: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10
```

---

## Russian regulation 2-col key-value table

Source: `98630_rus_20260706 (1).pdf` page 28

Raw OTSL:
```
<fcel>1<fcel>Наименование услугадателя<fcel>Территориальные органы полиции (далее – услугодатель)<nl><fcel>2<fcel>Способы предо-ставления госу-дарственной услуги<fcel>Прием документов и выдача результата оказания государственной услуги по всем подвидам осуществляется через:\n1) услугодателя;\n2) государственную корпорацию «Правительство для граждан» (далее – Государственная корпорация);\n3) веб-портал «электронного правительства» www.egov.kz, (далее – портал).<nl><fcel>3<fcel>Срок оказания государственной услуги<fcel>Со дня сдачи пакета необходимых документов услугодателю, в Государственную корпорацию и или через портал – 1 (один) рабочий день;\nмаксимально допустимое время ожидания для сдачи документов услугодателю и в Государственную корпорацию – 30 минут;\nмаксимально допустимое время обслуживания услугополучателя у услугодателя и в Государственной корпорации – 20 минут<nl><fcel>4<fcel>Форма оказания государственной услуги<fcel>Электронная (частично автоматизированная)/ бумажная<nl><fcel>5<fcel>Результат оказа-ния государственной услуги<fcel>Выдача разрешения на временное проживание в Республике Казахстан либо мотивированный ответ об отказе в оказании государственной услуги в случаях и по основаниям, предусмотренным пунктом 9 Пе-речия основных требований к оказанию государственной услуги.<nl><fcel>6<fcel>Размер оплаты, взимаемой с услугополучателя при оказании государственной услуги, и способы ее взимания в случаях, преду-смотренных законо-нодательством Республики Ка-захстан<fcel>Государственная услуга оказывается на бесплатной основе.<nl><fcel>7<fcel>график работы услугодателя, Го-сударственной корпорации и объектов информации<fcel>1) услугодателя – с понедельника по пятницу (с 9.00 до 18.30 часов, с перерывом на обед с 13.00 до 14.30 часов) кроме выходных (суббота, воскресенье) и праздичных дней, согласно трудовому законодательству Республикам Казахстан.\nПрием заявления и выдача результата оказания государственной услуги осуществляется услугодателем с понедельника по пятницу с 9.00 часов до 17.30 часов.\n2) Государственной корпорации - прием заявлений и выдача готовых результатов государственных услуг осуществляется через Государственную корпорацию с понедельника по пятницу включительно с 9.00 до 18.00 часов без перерыва, дежурные отделы обслуживания населения Государственной корпорации с понедельника по пятницу включительно с 9.00 до 20.00 часов и в субботу с 9.00 до 13.00 часов кроме празд-ничных и выходных дней согласно Трудового кодекса Республики Казахстан.<nl>
```

Raw text/grid (fcel text only, no repetition):
```
0: 1 | Наименование услугадателя | Территориальные органы полиции (далее – услугодатель)
1: 2 | Способы предо-ставления госу-дарственной услуги | Прием документов и выдача результата оказания государственной услуги по всем подвидам осуществляется через:\n1) услугодателя;\n2) государственную корпорацию «Правительство для граждан» (далее – Государственная корпорация);\n3) веб-портал «электронного правительства» www.egov.kz, (далее – портал).
2: 3 | Срок оказания государственной услуги | Со дня сдачи пакета необходимых документов услугодателю, в Государственную корпорацию и или через портал – 1 (один) рабочий день;\nмаксимально допустимое время ожидания для сдачи документов услугодателю и в Государственную корпорацию – 30 минут;\nмаксимально допустимое время обслуживания услугополучателя у услугодателя и в Государственной корпорации – 20 минут
3: 4 | Форма оказания государственной услуги | Электронная (частично автоматизированная)/ бумажная
4: 5 | Результат оказа-ния государственной услуги | Выдача разрешения на временное проживание в Республике Казахстан либо мотивированный ответ об отказе в оказании государственной услуги в случаях и по основаниям, предусмотренным пунктом 9 Пе-речия основных требований к оказанию государственной услуги.
5: 6 | Размер оплаты, взимаемой с услугополучателя при оказании государственной услуги, и способы ее взимания в случаях, преду-смотренных законо-нодательством Республики Ка-захстан | Государственная услуга оказывается на бесплатной основе.
6: 7 | график работы услугодателя, Го-сударственной корпорации и объектов информации | 1) услугодателя – с понедельника по пятницу (с 9.00 до 18.30 часов, с перерывом на обед с 13.00 до 14.30 часов) кроме выходных (суббота, воскресенье) и праздичных дней, согласно трудовому законодательству Республикам Казахстан.\nПрием заявления и выдача результата оказания государственной услуги осуществляется услугодателем с понедельника по пятницу с 9.00 часов до 17.30 часов.\n2) Государственной корпорации - прием заявлений и выдача готовых результатов государственных услуг осуществляется через Государственную корпорацию с понедельника по пятницу включительно с 9.00 до 18.00 часов без перерыва, дежурные отделы обслуживания населения Государственной корпорации с понедельника по пятницу включительно с 9.00 до 20.00 часов и в субботу с 9.00 до 13.00 часов кроме празд-ничных и выходных дней согласно Трудового кодекса Республики Казахстан.
```

---

## Tenancy checklist confirmed by TENANT

Source: `defence.pdf` page 26

Raw OTSL:
```
<ecel><fcel>Confirmed by TENANT<nl><fcel>Reviewed and explained the Tenancy Agreement (TA) terms & conditions in detail to the Tenant AND explained and answered all of the Tenant's questions in accordance with the provisions of the Tenancy Agreement.<ecel><nl><fcel>Provided the Tenant with a copy of the signed Tenancy Agreement.<ecel><nl><fcel>Reviewed, explained, and completed the "Joint Move-In" file together with the Tenant, and ensured it is duly signed.<ecel><nl><fcel>Verified and confirmed that the Keys and Access Cards issued (Pcs and Serial Codes) to the Tenant are accurate and fully with the Tenant Management System (TMS) Representative records.<ecel><nl><fcel>Confirmed that the Outstanding Balance has been fully paid into the correct account, and reminded the Tenant that all subsequent rental and payment obligations must be made only to the Tenant's individual account as per the Tenancy Agreement / Invoice issued.<ecel><nl><fcel>Confirmed that my Sales Person (Booking Consultant / Sales Agent) was ON-TIME and Present during the Move-In Tenancy Briefing together with the Tenant.<fcel>YES NO\nBC:\nAgent: a/r<nl><fcel>Explained to the Tenant that the Community Executive (CE) in charge may change from time to time, and that the Tenant must contact the CE in charge listed in the official WA House group for related matters.<ecel><nl><fcel>Conducted and uploaded the relevant information/docs (e.g. Move-In video (clear & multiple), NRICs/Passports, Workplace, Key Card images etc. into the official Google Drive folder.<fcel>NA<nl><fcel>I acknowledge that all the above steps have been duly completed and confirm my responsibility for their accuracy and fairness, and that all my actions were carried out fairly, honestly, and with integrity, in line with ICONIX SOP, the Anti-Bribery and Corruption Policy, and professional standards.<fcel>NA<nl><fcel>Signed by CE<ecel><nl><fcel>Name<ecel><nl><fcel>Date<ecel><nl>
```

Raw text/grid (fcel text only, no repetition):
```
 | Confirmed by TENANT
Reviewed and explained the Tenancy Agreement (TA) terms & conditions in detail to the Tenant AND explained and answered all of the Tenant's questions in accordance with the provisions of the Tenancy Agreement. | 
Provided the Tenant with a copy of the signed Tenancy Agreement. | 
Reviewed, explained, and completed the "Joint Move-In" file together with the Tenant, and ensured it is duly signed. | 
Verified and confirmed that the Keys and Access Cards issued (Pcs and Serial Codes) to the Tenant are accurate and fully with the Tenant Management System (TMS) Representative records. | 
Confirmed that the Outstanding Balance has been fully paid into the correct account, and reminded the Tenant that all subsequent rental and payment obligations must be made only to the Tenant's individual account as per the Tenancy Agreement / Invoice issued. | 
Confirmed that my Sales Person (Booking Consultant / Sales Agent) was ON-TIME and Present during the Move-In Tenancy Briefing together with the Tenant. | YES NO\nBC:\nAgent: a/r
Explained to the Tenant that the Community Executive (CE) in charge may change from time to time, and that the Tenant must contact the CE in charge listed in the official WA House group for related matters. | 
Conducted and uploaded the relevant information/docs (e.g. Move-In video (clear & multiple), NRICs/Passports, Workplace, Key Card images etc. into the official Google Drive folder. | NA
I acknowledge that all the above steps have been duly completed and confirm my responsibility for their accuracy and fairness, and that all my actions were carried out fairly, honestly, and with integrity, in line with ICONIX SOP, the Anti-Bribery and Corruption Policy, and professional standards. | NA
Signed by CE | 
Name | 
Date | 
```

---

## Tenancy checklist confirmed by CE (different table, adjacent page)

Source: `defence.pdf` page 27

Raw OTSL:
```
<ecel><fcel>Confirmed by CE<nl><fcel>I am PRESENT during the Tenancy Briefing and Joint Move-In Briefing together with the Community Executive (CE).<ecel><nl><fcel>I explained and answered all of the Tenant's questions in accordance with the provisions of the Tenancy Agreement.<ecel><nl><fcel>I've provided the Tenant with a copy of the signed Booking Form.<ecel><nl><fcel>I've ensured that the Move-In Date and Rental Start Date are accurate and tally with this document. Eg. TODAY's date is the Move-In Date on file.<ecel><nl><fcel>(For Sales Representative only: Name ______)
• I have the required knowledge to answer all Tenant's questions based on TA T&C
• I have [ attended / will attend ] the official ICONIX Briefing<fcel>Representative Capability:
______%<nl><fcel>(For Booking Consultant only)
• I've conducted the Move-In briefing and ensured the Move-In video was taken, approved by the CE, and uploaded into the approved Google Drive folder.<ecel><nl><fcel>I acknowledge that all the above steps have been duly completed and confirm my responsibility for their accuracy and fairness, and that all my actions were carried out fairly, honestly, and with integrity, in line with ICONIX SOP, the Anti-Bribery and Corruption Policy, and professional standards.
Signed by BC/Agent : ________________
Name : \(\underline{\text{CHEEZHAN YMAO}}\)
Date : \(\underline{\text{24/1/2026}}\)
Note: This document is a Required Document for the purpose of Incentive / Commission payment and calculations by the Accounts Department. Failure to complete or submit this document may result in the withholding or adjustment of incentive/commission entitlements.<fcel>NA<nl>
```

Raw text/grid (fcel text only, no repetition):
```
 | Confirmed by CE
I am PRESENT during the Tenancy Briefing and Joint Move-In Briefing together with the Community Executive (CE). | 
I explained and answered all of the Tenant's questions in accordance with the provisions of the Tenancy Agreement. | 
I've provided the Tenant with a copy of the signed Booking Form. | 
I've ensured that the Move-In Date and Rental Start Date are accurate and tally with this document. Eg. TODAY's date is the Move-In Date on file. | 
(For Sales Representative only: Name ______)
• I have the required knowledge to answer all Tenant's questions based on TA T&C
• I have [ attended / will attend ] the official ICONIX Briefing | Representative Capability:
______%
(For Booking Consultant only)
• I've conducted the Move-In briefing and ensured the Move-In video was taken, approved by the CE, and uploaded into the approved Google Drive folder. | 
I acknowledge that all the above steps have been duly completed and confirm my responsibility for their accuracy and fairness, and that all my actions were carried out fairly, honestly, and with integrity, in line with ICONIX SOP, the Anti-Bribery and Corruption Policy, and professional standards.
Signed by BC/Agent : ________________
Name : \(\underline{\text{CHEEZHAN YMAO}}\)
Date : \(\underline{\text{24/1/2026}}\)
Note: This document is a Required Document for the purpose of Incentive / Commission payment and calculations by the Accounts Department. Failure to complete or submit this document may result in the withholding or adjustment of incentive/commission entitlements. | NA
```

---

## Agreement terms table (page 1 of 3-page merge)

Source: `defence.pdf` page 47

Raw OTSL:
```
<fcel>Section<fcel>Item<fcel>Particulars<nl><fcel>1<fcel>Date of Agreement<fcel>31 Jan 2026<nl><fcel>2<fcel>The Manager<fcel>ICONIX Co-Living Sdn Bhd (1439156-A)\n20-01, Mercu Aspire, No 3, Jalan Bangsar, KL Eco City, 59200, Kuala Lumpur, Malaysia\nContact: +603 8681 2519<nl><fcel>2A<fcel>The Owner<fcel>Name of the Registered/ Beneficial Owner/ Authorized Agent\nAu Wei Shinn\nNRIC No. (if individual)/Company No. (if company)\n910823045369<nl><fcel>3<fcel>The Licensee<fcel>Licensee 1 (Primary Licensee)\nName: MD. MASUM BILLAL\nCountry/Nationality: Bangladeshi\nNRIC/Passport: B00580030\nEmail: billalmasumg93@gmail.com\nContact(s): 60166809511\nTenant/Licensee Acc: MD.Mg511-01\nNote: All invoices and communications will be sent through the Primary Licensee's Email and Local contact number. The Tenant must ensure that any changes to the email or contact number are promptly updated.<nl><fcel>4<fcel>The Premises<fcel>Premise Name: The Colony by Infinitum\nPremise Address: A-12-07, Wisma Infinitum, No 18, Jalan Dewan Sultan Sulaiman, Kuala Lumpur, 50300, Federal Territory of Kuala Lumpur, Malaysia\nRented in PARTS/WHOLE\nThis Premise is Rented in PARTS (Multiple Tenancy)<nl><fcel>4A<fcel>The said Space<fcel>Unit Number: A-12-07\nRoom Code: S1 (P)<nl><fcel>5<fcel>License Period/Term<fcel>12 months<nl><fcel>6<fcel>Commencement Date<fcel>31 Jan 2026<nl>
```

Raw text/grid (fcel text only, no repetition):
```
Section | Item | Particulars
1 | Date of Agreement | 31 Jan 2026
2 | The Manager | ICONIX Co-Living Sdn Bhd (1439156-A)\n20-01, Mercu Aspire, No 3, Jalan Bangsar, KL Eco City, 59200, Kuala Lumpur, Malaysia\nContact: +603 8681 2519
2A | The Owner | Name of the Registered/ Beneficial Owner/ Authorized Agent\nAu Wei Shinn\nNRIC No. (if individual)/Company No. (if company)\n910823045369
3 | The Licensee | Licensee 1 (Primary Licensee)\nName: MD. MASUM BILLAL\nCountry/Nationality: Bangladeshi\nNRIC/Passport: B00580030\nEmail: billalmasumg93@gmail.com\nContact(s): 60166809511\nTenant/Licensee Acc: MD.Mg511-01\nNote: All invoices and communications will be sent through the Primary Licensee's Email and Local contact number. The Tenant must ensure that any changes to the email or contact number are promptly updated.
4 | The Premises | Premise Name: The Colony by Infinitum\nPremise Address: A-12-07, Wisma Infinitum, No 18, Jalan Dewan Sultan Sulaiman, Kuala Lumpur, 50300, Federal Territory of Kuala Lumpur, Malaysia\nRented in PARTS/WHOLE\nThis Premise is Rented in PARTS (Multiple Tenancy)
4A | The said Space | Unit Number: A-12-07\nRoom Code: S1 (P)
5 | License Period/Term | 12 months
6 | Commencement Date | 31 Jan 2026
```

---

## Agreement terms table (page 2 of 3-page merge, same header)

Source: `defence.pdf` page 48

Raw OTSL:
```
<fcel>Section<fcel>Item<fcel>Particulars<nl><fcel>6A<fcel>Expiration Date<fcel>30 Jan 2027\nIn the event there is early termination before the Expiry Date, the Licensee shall be deemed to be in breach and default of this Agreement whereupon the Owner, shall be entitled to forthwith re-enter upon the Demised Premises or any part thereof and forfeit absolutely the Security Deposit.<nl><fcel>6B<fcel>Renewal Term<fcel>License Agreement is automatically renewed for a term of six months, and on a bi-annual basis (automatically every 6-months) upon expiry of your Tenancy Term.\nThere shall be no lock in period during the Renewal Term or terms thereafter and the Parties may terminate the License by providing 1-month prior written notice to the other Party without any penalties or forfeiture of deposits.<nl><fcel>7<fcel>Monthly Rental<fcel>Monthly Rental Rate: RM 2,000.00\nRinggit Malaysia Two Thousand only\n\(^{{*}}\) Inclusive of RM 50.00 managed services\nNote: payable by the first \(({{}^{{th}}})\) day of every month, and shall not be paid later than the seventh \(({{}^{{th}}})\) day of the month. For the avoidance of doubt, the Monthly Rental shall only be considered duly paid if it is reflected in the Company's bank statement on or before the Seventh \(({{}^{{th}}})\) day of the month. Licensees are hereby cautioned to account for processing times, especially in cases involving interbank or international transfers, to ensure adherence to this payment deadline.<nl><fcel>7A<fcel>Late Payment Charge<fcel>Ringgit Malaysia Three Hundred (RM 300.00) only<nl><fcel>8<fcel>Security Deposit (held with the Owner)<fcel>Ringgit Malaysia Four Thousand (RM 4,000.00) only\nbeing 2.0 Month Security Deposit<nl><fcel>8A<fcel>Access Card Deposit<fcel>Ringgit Malaysia Two Hundred (RM 200.00) only\nbeing 1 pcs of access cards\nCard Number/ID: 0A02 (07702)<nl><fcel>8B<fcel>Key Deposit<fcel>N/A\nNote: Deposit will be returned in full upon the move-out and subsequent return of Keys & Key Tags (with barcodes) in good condition.<nl><fcel>8C<fcel>Other Deposit<fcel>N/A<nl><fcel>9<fcel>Utilities Deposit (held with the Owner)<fcel>Ringgit Malaysia One Thousand (RM 1,000.00) only<nl>
```

Raw text/grid (fcel text only, no repetition):
```
Section | Item | Particulars
6A | Expiration Date | 30 Jan 2027\nIn the event there is early termination before the Expiry Date, the Licensee shall be deemed to be in breach and default of this Agreement whereupon the Owner, shall be entitled to forthwith re-enter upon the Demised Premises or any part thereof and forfeit absolutely the Security Deposit.
6B | Renewal Term | License Agreement is automatically renewed for a term of six months, and on a bi-annual basis (automatically every 6-months) upon expiry of your Tenancy Term.\nThere shall be no lock in period during the Renewal Term or terms thereafter and the Parties may terminate the License by providing 1-month prior written notice to the other Party without any penalties or forfeiture of deposits.
7 | Monthly Rental | Monthly Rental Rate: RM 2,000.00\nRinggit Malaysia Two Thousand only\n\(^{{*}}\) Inclusive of RM 50.00 managed services\nNote: payable by the first \(({{}^{{th}}})\) day of every month, and shall not be paid later than the seventh \(({{}^{{th}}})\) day of the month. For the avoidance of doubt, the Monthly Rental shall only be considered duly paid if it is reflected in the Company's bank statement on or before the Seventh \(({{}^{{th}}})\) day of the month. Licensees are hereby cautioned to account for processing times, especially in cases involving interbank or international transfers, to ensure adherence to this payment deadline.
7A | Late Payment Charge | Ringgit Malaysia Three Hundred (RM 300.00) only
8 | Security Deposit (held with the Owner) | Ringgit Malaysia Four Thousand (RM 4,000.00) only\nbeing 2.0 Month Security Deposit
8A | Access Card Deposit | Ringgit Malaysia Two Hundred (RM 200.00) only\nbeing 1 pcs of access cards\nCard Number/ID: 0A02 (07702)
8B | Key Deposit | N/A\nNote: Deposit will be returned in full upon the move-out and subsequent return of Keys & Key Tags (with barcodes) in good condition.
8C | Other Deposit | N/A
9 | Utilities Deposit (held with the Owner) | Ringgit Malaysia One Thousand (RM 1,000.00) only
```

---

## Agreement terms table (page 3 of 3-page merge, same header)

Source: `defence.pdf` page 49

Raw OTSL:
```
<fcel>Section<fcel>Item<fcel>Particulars<nl><fcel>9A<fcel>Utilities Bills\(^{{*}}\)\n\n(*select whichever that is applicable)<fcel>Included\n'Fair Usage Policy applies.<nl><ucel><ucel><fcel>Payment according to individual meter\n✓\nSubsidy of 0.00 (unit kwl)\n\n'The charges for the sub-meter of the Premises/Individual Room is Ringgit Malaysia Fifty Five Sen (RM 0.55 ) only per unit kwl.<nl><fcel>9B<fcel>Water Bills\(^{{*}}\)<fcel>Payment to be shared between occupants of the Premises, with a subsidy of RM ______ from the Owner<nl><ucel><ucel><fcel>Payable by Landlord\nWater charges shall be settled either through the Joint Management Body (JMB)/Building Management or directly remitted to Syarikat Bekalan Air Selangor (SYABAS)/Air Selangor, in accordance with the established payment procedures for such utilities.<nl><ucel><ucel><fcel>Syabas/Air Selangor Provider: SYABAS Account Number: Pay to JMB<nl><fcel>9C<fcel>Common Area Utilities<ecel><nl><fcel>10<fcel>Payment Details<fcel>All payment shall be made in Ringgit Malaysia (RM) and shall be paid promptly to the account:\nAccount Holder\nICONIX Co-Living Sdn Bhd\nName of Bank\nCIMB Bank Berhad\nAccount No.\n98-300-0121-67363\nImportant Notice: Please make all your payments to this Exact Account Number to avoid any delays or complications with your tenancy. Using this unique account number ensures a smooth and efficient payment process.\n**Please always quote your Tenant Account: MD.M9511-01 when making payments.<nl>
```

Raw text/grid (fcel text only, no repetition):
```
Section | Item | Particulars
9A | Utilities Bills\(^{{*}}\)\n\n(*select whichever that is applicable) | Included\n'Fair Usage Policy applies.
 |  | Payment according to individual meter\n✓\nSubsidy of 0.00 (unit kwl)\n\n'The charges for the sub-meter of the Premises/Individual Room is Ringgit Malaysia Fifty Five Sen (RM 0.55 ) only per unit kwl.
9B | Water Bills\(^{{*}}\) | Payment to be shared between occupants of the Premises, with a subsidy of RM ______ from the Owner
 |  | Payable by Landlord\nWater charges shall be settled either through the Joint Management Body (JMB)/Building Management or directly remitted to Syarikat Bekalan Air Selangor (SYABAS)/Air Selangor, in accordance with the established payment procedures for such utilities.
 |  | Syabas/Air Selangor Provider: SYABAS Account Number: Pay to JMB
9C | Common Area Utilities | 
10 | Payment Details | All payment shall be made in Ringgit Malaysia (RM) and shall be paid promptly to the account:\nAccount Holder\nICONIX Co-Living Sdn Bhd\nName of Bank\nCIMB Bank Berhad\nAccount No.\n98-300-0121-67363\nImportant Notice: Please make all your payments to this Exact Account Number to avoid any delays or complications with your tenancy. Using this unique account number ensures a smooth and efficient payment process.\n**Please always quote your Tenant Account: MD.M9511-01 when making payments.
```
