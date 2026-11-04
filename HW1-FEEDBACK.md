# HW 1 feedback

## 100 / 100

Good work. Putting the recap on a RabbitMQ queue so nothing happens on the request thread was the right call, and only checking every fifth message in a topic with a memcached add-if-absent is a clean way to keep the cost down. Returning a structured ref per conversation rather than a built URL is tidy too.

## Marks

| | Points | Awarded |
| --- | --- | --- |
| Submission link | 10 | **10** |
| Installs and runs from README.md | 20 | **20** |
| No private credentials in the repository | 10 | **10** |
| Message recap backend | 10 | **10** |
| Message recap frontend, with a video | 10 | **10** |
| Topic title improver backend | 10 | **10** |
| Topic title improver frontend, with a video | 10 | **10** |
| Explanation to course staff (provisional) | 20 | **20** |
| **Total** | **100** | **100** |

Marked `fa3c61c8`, your last commit before the 20 September deadline (2026-09-19).

The 20 points for the explanation are **provisional**. They are awarded for now so grades could go out, and your mark is finalised after the demo session. Everything else is settled.

Grade of record is on Canvas. If something here looks wrong, reply on the commit or send me a message and I'll take another look.
