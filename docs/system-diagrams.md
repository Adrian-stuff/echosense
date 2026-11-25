# EchoSense system diagrams and comparison

## Figure 1. Context-level diagram

![EchoSense exchanges data with a child user, a parent or caregiver, and a Kinect or test depth source.](figures/echosense-context-dfd.svg)

The context diagram treats the assistive device and parent dashboard as a single system. The sensor provides depth frames and supported color. EchoSense sends cues and spoken alerts to the child. The parent or caregiver manages access and fitting, supervises training, enters the child’s pointing responses, and reviews footage and reports.

## Figure 2. Level 1 data-flow diagram

![Five processes show authentication, child fitting, sensing and audio, supervised practice, and footage and reports. Labeled arrows connect the processes to external entities and data stores.](figures/echosense-level-1-dfd.svg)

| Process | What it does | Main data store |
|---|---|---|
| 1.0 Authenticate and manage access | Verifies logins and invitations, resolves permissions, and issues or revokes sessions. | D1 · Device SQLite |
| 2.0 Configure child fitting | Uses measurements and optional floor calibration to preview coverage and save the fitting. | D2 · Device settings JSON |
| 3.0 Process sensing and render cues | Turns depth measurements into obstacle and drop-off detections, selects live or training cues, and streams live views and device status. | Reads fitting from 2.0 |
| 4.0 Run supervised practice | Plays virtual targets, records caregiver-entered responses, and pauses on loss of supervision. Confirmed results feed progress reporting. | D3 · Device SQLite |
| 5.0 Manage footage, backups and reports | Captures clips and snapshots in the browser, links them to practice, produces reports, and exports or restores backups. | D4 · Browser IndexedDB and recovery drafts |

See the [whole-system operational flowchart](system-flowchart.md) for decision paths, faults, and recovery.

## Table 1. Comparison of EchoSense and related assistive tools

| System | Primary focus | Form and interface | Obstacle sensing | User feedback | Route guidance | Practice tracking | Caregiver records |
|---|---|---|---|---|---|---|---|
| **EchoSense prototype** | Obstacle assistance and supervised practice | Chest-mounted Kinect and local web dashboard | Depth-based obstacle and drop-off cues | Spatial tones and spoken alerts | No | Direction trials, goals, and reports | Local clips, notes, and ZIP backup |
| NOA by biped.ai [1] | Wearable mobility assistance | Camera-equipped wearable and headphones | Camera-based obstacle detection | Spatial obstacle sounds and voice guidance | GPS directions | ND | ND |
| WeWALK Smart Cane 2 [2] | Smart cane mobility and wayfinding | Electronic white cane and companion app | Above-chest sensor detection | Audio and vibration alerts | Via companion app | ND | ND |
| Microsoft Seeing AI [3] | Text reading and scene description | Phone camera app | ND for real-time obstacle alerts | Spoken text and scene descriptions | ND | ND | ND |
| Lazarillo [4] | Accessible wayfinding and exploration | Phone GPS app | ND for nearby obstacle alerts | Spoken directions and place announcements | Turn-by-turn routes | ND | ND |

**ND** means the reviewed product materials do not document the feature. The table compares feature coverage. EchoSense entries describe the implemented prototype. [Download the vector table](figures/echosense-comparison.svg).

1. biped.ai. [NOA product overview](https://biped.ai/) and [orientation and mobility guide](https://www.biped.ai/en/o-m-guide/).
2. WeWALK. [Smart Cane 2 user manual](https://support.wewalk.io/en/article/wewalk-smart-cane-2-users-manual/).
3. Microsoft. [Seeing AI product overview](https://www.microsoft.com/en-us/garage/wall-of-fame/seeing-ai/).
4. Lazarillo. [Lazarillo App overview](https://lazarillo.app/theapp/).
