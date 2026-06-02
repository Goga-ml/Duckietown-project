# Bug-Hunter Reviewer Memory — KvatiTown / Duckietown project

- [Deploy packaging gap](project_deploy_packaging.md) — `--run` only ships the named task's packages; cross-task imports break on the bot.
- [Cross-task coupling in traffic_signs](project_traffic_signs_coupling.md) — behavior layer imports object_detection + visual_lane_servoing packages.
- [Traffic-signs FSM review notes](project_traffic_signs_fsm.md) — bug-prone areas in the Person B behaviour state machine (cooldown, snap-to-zero).
- [Detector coordinate conventions](project_detector_coords.md) — detect() returns bboxes in INPUT-frame space; square the frame for should_stop reuse.
- [Frame-skip None contract](project_frame_skip.md) — detect() returns None on skipped frames (default skip=1); keep last reading, don't report clear.
- [Threading model traffic_signs](project_threading_traffic_signs.md) — daemon loops, lock-guarded state, uncaught-exception thread-death + reset gaps.
- [Server control-loop concurrency](project_server_concurrency.md) — control loop runs in visualize() on MJPEG thread; unlocked running/manual_mode/_last_tick_t/_prev_state; no /video single-client guard.
