# MONDAY_BOARD_MAPPING — "For Social Media" (5105608159)

VERIFIED via monday.com connector, read-only. Board kind: shareable, workspace "Main workspace", 164 items, classic hierarchy, subitems on board <subitems-board>.

## Groups

| Group ID | Title | Items |
|---|---|---|
| `topics` | For Posts | 26 |
| `group_mm7xagm` | For Stories | 105 |
| `group_title` | Posted | 15 |
| `group_mm7y8mkr` | Skipped | 18 |

## Columns

| Column ID | Title | Type | Role | Filled (of 164) | Likely writer |
|---|---|---|---|---|---|
| `name` | Name | name | item title | 164 | human / source copy |
| `text_mm7xqn4e` | Code | text | client/style code | — | source copy |
| `status` | Status | status | combined visible status | 164 | **n8n + humans (conflict)** |
| `color_mm7xm9b6` | Format | status (Story/Post) | content format | 164 | humans (+ n8n on create) |
| `color_mm7xe2j2` | Topazed | status (Topazed/Not yet) | Topaz confirmation | 164 | humans / editor |
| `date4` | Post Date | date | legacy schedule | 67 | legacy scheduler / humans |
| `hour_mm7xy9cf` | Post Time | hour | legacy schedule | 66 | legacy scheduler / humans |
| `date_mm7y8s9t` | Publish at | date+time | V2 unified slot (Cairo) | **0** | V2 (not yet used) |
| `long_text_mm7x2ay1` | Caption | long text | Post caption | — | AI caption flow / humans |
| `link_mm7x8dy7` | Dropbox Link | link | latest version | — | n8n |
| `link_mm7xaep2` | Folder Link | link | project folder | — | n8n |
| `link_mm7ywc0w` | Publish video | link | approved final media | 103 | n8n |
| `link_mm7xb56a` | Post Link | link | proof of publication | — | publisher |
| `text_mm7xemtq` | Version Check | text | chosen file + confidence | — | n8n |
| `text_mm7yy451` | Source asset version | text | Dropbox file ID + rev | 101 | n8n |
| `text_mm7yp8h` | Verified media ID | text | verification record ID | — | n8n |
| `text_mm7zjt4m` | Video measurements | text | duration/res/size | — | n8n |
| `text_mm7z139h` | Processed format | text | detects Story↔Post change | 49 | n8n |
| `date_mm7zd2b9` | Last checked | date | last verification | — | n8n |
| `long_text_mm7ysrbz` | System update | long text | automation status | — | n8n |
| `long_text_mm7zbtbn` | المطلوب منك | long text | required action | — | n8n |
| `date_mm7yr4h3` | Published at | date+time | confirmed publish time | **0** | publisher |
| `text_mm7yfqhb` | Instagram media ID | text | publish receipt | **0** | publisher |
| `text_mm7y4h4a` | Source item ID | text | source project link | — | n8n |
| `text_mm7y5kd1` | Style | text | from Code prefix | — | n8n |
| `text_mm7yjmqd` | Story variety | text | in-client variety | — | n8n / humans |
| `multiple_person_mm7yy4tk` | Social owner | people | ownership | — | n8n / humans |
| `text_mm7yhjf1` | IG Colab | text | collab handle | — | humans |
| `text_mm7xaf3t` | Notes | text | human notes | — | humans |
| `numeric_mm7xade9` | Numbers | numbers | unknown | — | unknown |

## Status labels (column `status`)

Unscheduled(5), Redy For Scheduled(2), Working on it(0), Scheduled(3), Posted(4, done), Skipped(10), Waiting for Editor(8), Done(1, deactivated), Publishing(6), Paused(17), Needs Review(7), "جاري فحص الفيديو"(16, checking video), "مقبول كبوست"(9, accepted as post), "ستوري طويل"(11, long story).

Current distribution: Waiting for Editor 46 · Skipped 44 · Unscheduled 29 · Needs Review 25 · Posted 15 · ستوري طويل 5 · **Scheduled 0**.

## Board automations (all inactive)

1. Status → Posted ⇒ move to group Posted.
2. Format → Story and Status ≠ Posted ⇒ move to For Stories.
3. Format → Post and Status ≠ Posted ⇒ move to For Posts.

Webhooks / installed apps: NOT VERIFIED (connector does not expose them). n8n "(Office)" workflows use board webhooks on other boards (INFERRED).

## Writer analysis

- **VERIFIED:** the last 1,000 activity events on this board were all made by one user — the account "Waset Co Studio" (owner). n8n's monday credential and human edits share this identity. Writer attribution by actor is impossible today.
- **INFERRED:** automation writes are recognisable only by timing bursts and machine text.

## Inconsistencies found (VERIFIED)

1. 44 items have Status `Skipped` but only 18 are in the Skipped group (26 Skipped items sit in For Stories / For Posts).
2. 15 Posted items have no `Instagram media ID` and no `Published at` — no durable receipt.
3. `Publish at` is empty everywhere while legacy `Post Date/Post Time` are filled on 67 items → two scheduling representations.
4. "مقبول كبوست" (accepted as post) is a status label, while format lives in `Format` — risk of implicit Story→Post conversion being expressed through status (must stay owner-approved).
5. 24 Scheduled → Needs Review transitions today; prior reservations survive in the n8n data table (see inventory).
