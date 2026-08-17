# 3D Modeling Token Consumption Statistics — qwen3.8 (cad skill)

Single-agent `cad skill` benchmark with `qwen3.8-max` (10 prompts P1–P10). Companion file to `qwen3.7_token.md` (same skill, `qwen3.7-max`).

## Unit Price Reference

Model: `qwen3.8-max` (standard pricing, 2× qwen3.7; qwen3.7 currently on limited-time discount).

| Item | Unit Price (CNY / million tokens) |
|---|---:|
| input | 12 |
| cache_creation (cache_w) | 15 |
| cache_read (cache_r) | 1.2 |
| output | 36 |

## P1
 # calls      input    cache_w      cache_r     output        total      cost  prompt
------------------------------------------------------------------------------------------------------------------------
 1     2     49,344          0            0        364       49,708    0.61元  Create a single solid STEP model in millimeters. The part is
 2    11    328,598          0            0      3,604      332,202    4.07元  Base directory for this skill: /Users/puma/.claude/skills/ca
 3     0          0          0            0          0            0    0.00元  [Request interrupted by user]
------------------------------------------------------------------------------------------------------------------------
      13    377,942          0            0      3,968      381,910    4.68元  TOTAL

## P2
 # calls      input    cache_w      cache_r     output        total      cost  prompt
------------------------------------------------------------------------------------------------------------------------
 1     2     49,466          0            0         88       49,554    0.60元  Create a single solid circular flange as a STEP model in mil
 2    76    216,287          0    3,249,664     49,624    3,515,575    8.28元  Base directory for this skill: /Users/puma/.claude/skills/ca
 3     9      4,865          0      561,024      2,633      568,522    0.83元  Base directory for this skill: /Users/puma/.claude/skills/ca
------------------------------------------------------------------------------------------------------------------------
      87    270,618          0    3,810,688     52,345    4,133,651    9.70元  TOTAL

## P3
 # calls      input    cache_w      cache_r     output        total      cost  prompt
------------------------------------------------------------------------------------------------------------------------
 1     2     49,754          0            0         88       49,842    0.60元  Create a single solid L-bracket STEP model in millimeters. T
 2   111    819,510          0    9,634,432    340,735   10,794,677   33.66元  Base directory for this skill: /Users/puma/.claude/skills/ca
 3    15      8,706          0    1,827,968      4,784    1,841,458    2.47元  Base directory for this skill: /Users/puma/.claude/skills/ca
------------------------------------------------------------------------------------------------------------------------
     128    877,970          0   11,462,400    345,607   12,685,977   36.73元  TOTAL

## P4
 # calls      input    cache_w      cache_r     output        total      cost  prompt
------------------------------------------------------------------------------------------------------------------------
 1     2     49,564          0            0         72       49,636    0.60元  Create a single solid stepped shaft STEP model in millimeter
 2   108    223,687          0    5,991,424     93,428    6,308,539   13.24元  Base directory for this skill: /Users/puma/.claude/skills/ca
 3    13      7,453          0    1,056,128      3,704    1,067,285    1.49元  Base directory for this skill: /Users/puma/.claude/skills/ca
------------------------------------------------------------------------------------------------------------------------
     123    280,704          0    7,047,552     97,204    7,425,460   15.32元  TOTAL

## P5
 # calls      input    cache_w      cache_r     output        total      cost  prompt
------------------------------------------------------------------------------------------------------------------------
 1     2     49,550          0            0         68       49,618    0.60元  The outer shape is a rectangular box 100 mm long in X, 70 mm
 2    95    138,099          0    4,874,880     66,075    5,079,054    9.89元  Base directory for this skill: /Users/puma/.claude/skills/ca
 3    10      5,477          0      673,408      2,305      681,190    0.96元  Base directory for this skill: /Users/puma/.claude/skills/ca
------------------------------------------------------------------------------------------------------------------------
     107    193,126          0    5,548,288     68,448    5,809,862   11.44元  TOTAL

## P6
 # calls      input    cache_w      cache_r     output        total      cost  prompt
------------------------------------------------------------------------------------------------------------------------
 1     2     49,808          0            0        628       50,436    0.62元  Create a single solid aerospace-style clevis bracket as a ST
 2   144    536,212          0   11,735,040    169,310   12,440,562   26.61元  Base directory for this skill: /Users/puma/.claude/skills/ca
 3     8      4,159          0    1,064,448      1,869    1,070,476    1.39元  Base directory for this skill: /Users/puma/.claude/skills/ca
------------------------------------------------------------------------------------------------------------------------
     154    590,179          0   12,799,488    171,807   13,561,474   28.63元  TOTAL

## P7
 # calls      input    cache_w      cache_r     output        total      cost  prompt
------------------------------------------------------------------------------------------------------------------------
 1     2         12     51,192            0        670       51,874    0.79元  Create a single solid radial-engine-style cylinder as a STEP
 2   109        654    128,234    6,403,036     70,227    6,602,151   12.14元  Base directory for this skill: /Users/puma/.claude/skills/ca
 3    10         60      4,828      802,572      3,026      810,486    1.15元  Base directory for this skill: /Users/puma/.claude/skills/ca
------------------------------------------------------------------------------------------------------------------------
     121        726    184,254    7,205,608     73,923    7,464,511   14.08元  TOTAL

## P8
 # calls      input    cache_w      cache_r     output        total      cost  prompt
------------------------------------------------------------------------------------------------------------------------
 1     2     49,664          0            0        162       49,826    0.60元  Create a single solid centrifugal impeller as a STEP model i
 2   126    663,406          0   10,175,104    177,678   11,016,188   26.57元  Base directory for this skill: /Users/puma/.claude/skills/ca
 3     9      5,027          0    1,121,536      2,631    1,129,194    1.50元  Base directory for this skill: /Users/puma/.claude/skills/ca
------------------------------------------------------------------------------------------------------------------------
     137    718,097          0   11,296,640    180,471   12,195,208   28.67元  TOTAL

## P9
 # calls      input    cache_w      cache_r     output        total      cost  prompt
------------------------------------------------------------------------------------------------------------------------
 1     2     49,650          0            0        568       50,218    0.62元  Create a single STEP model of a miniature spiral staircase i
 2   117    232,805          0    9,117,952    151,243    9,502,000   19.18元  Base directory for this skill: /Users/puma/.claude/skills/ca
 3    11      6,012          0    1,262,336      3,027    1,271,375    1.70元  Base directory for this skill: /Users/puma/.claude/skills/ca
------------------------------------------------------------------------------------------------------------------------
     130    288,467          0   10,380,288    154,838   10,823,593   21.49元  TOTAL

## P10
 # calls      input    cache_w      cache_r     output        total      cost  prompt
------------------------------------------------------------------------------------------------------------------------
 1     3     74,640          0            0        930       75,570    0.93元  Create a visually clear simplified planetary gear assembly a
 2   106    331,835          0   10,917,632    237,702   11,487,169   25.64元  Base directory for this skill: /Users/puma/.claude/skills/ca
 3    10      5,762          0    1,407,872      2,747    1,416,381    1.86元  Base directory for this skill: /Users/puma/.claude/skills/ca
------------------------------------------------------------------------------------------------------------------------
     119    412,237          0   12,325,504    241,379   12,979,120   28.43元  TOTAL

## Summary (P1–P10)

| Prompt | API calls | input | cache_w | cache_r | output | total | Cost (CNY) |
|---|---:|---:|---:|---:|---:|---:|---:|
| P1 | 13 | 377,942 | 0 | 0 | 3,968 | 381,910 | **4.68** |
| P2 | 87 | 270,618 | 0 | 3,810,688 | 52,345 | 4,133,651 | **9.70** |
| P3 | 128 | 877,970 | 0 | 11,462,400 | 345,607 | 12,685,977 | **36.73** |
| P4 | 123 | 280,704 | 0 | 7,047,552 | 97,204 | 7,425,460 | **15.32** |
| P5 | 107 | 193,126 | 0 | 5,548,288 | 68,448 | 5,809,862 | **11.44** |
| P6 | 154 | 590,179 | 0 | 12,799,488 | 171,807 | 13,561,474 | **28.63** |
| P7 | 121 | 726 | 184,254 | 7,205,608 | 73,923 | 7,464,511 | **14.08** |
| P8 | 137 | 718,097 | 0 | 11,296,640 | 180,471 | 12,195,208 | **28.67** |
| P9 | 130 | 288,467 | 0 | 10,380,288 | 154,838 | 10,823,593 | **21.49** |
| P10 | 119 | 412,237 | 0 | 12,325,504 | 241,379 | 12,979,120 | **28.43** |
| **TOTAL** | **1,119** | **4,010,066** | **184,254** | **81,876,456** | **1,389,990** | **87,460,766** | **199.18** |

## Pass Rate (P1–P10)

| Prompt | Pass Rate | Notes |
|---|---|---|
| P1 | 7/7 | ✓ |
| P2 | 10/10 | ✓ |
| P3 | 14/14 | ✓ |
| P4 | 11/11 | ✓ |
| P5 | 12/12 | ✓ |
| P6 | 14/18 | 筋板对称轴错（#15: spec 要求 +Y/-Y 关于 Y 轴对称，实际关于 X 轴对称 +X/-X，旋转 90°）；筋板连接位置错（#14: 应贴立耳外侧，实际脱离立耳）；凭空多出 2 个筋板；底板圆角位置错（#16/#18: 应上下底面都倒，skill 只倒了下面 + 多倒了侧面竖棱）|
| P7 | 16/17 | 底部法兰盘到第一个散热板的间距过大（立柱过长或散热板位置偏移）|
| P8 | 15/15 | ✓ |
| P9 | 14/16 | 扶手与支撑柱（baluster）之间有微小缝隙，未真正连接，模型为多实体而非单一实体 |
| P10 | 19/21 | 中心齿轮通孔未打穿；行星齿轮固定栓出现在齿轮下方而非齿轮中间 |
| **TOTAL** | **132/141** | **93.6%** |

Note: qwen3.8 model success rate (132/141, 93.6%) is **lower** than qwen3.7 (138/141, 97.9%) despite qwen3.8's 2× standard price — see `qwen3.7_token.md` for the qwen3.7 baseline.
