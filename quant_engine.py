def _refresh_signal_entry_gate(self, sig: CommittedSignal, mid_price: float) -> None:
        tp1_dist = abs(sig.tp1 - sig.entry_price)
        sl_dist = abs(sig.entry_price - sig.stop_loss)
        if tp1_dist <= 1e-9 or sl_dist <= 1e-9:
            return

        if sig.direction == "LONG":
            drift = mid_price - sig.entry_price
            if mid_price >= sig.tp1:
                sig.entry_status = "TP1 REACHED — TRACKING REMAINING TARGETS"
                sig.can_enter = False
            elif drift < -sl_dist:
                sig.entry_status = "STOP-LOSS ZONE REACHED"
                sig.can_enter = False
            elif drift > tp1_dist * 0.65:
                sig.entry_status = "EXTENDED MOVE — WATCH PULLBACK OR NEXT SETUP"
                sig.can_enter = True
            else:
                sig.entry_status = "EXECUTE NOW — OPTIMAL ENTRY ZONE"
                sig.can_enter = True
        else:
            drift = sig.entry_price - mid_price
            if mid_price <= sig.tp1:
                sig.entry_status = "TP1 REACHED — TRACKING REMAINING TARGETS"
                sig.can_enter = False
            elif drift < -sl_dist:
                sig.entry_status = "STOP-LOSS ZONE REACHED"
                sig.can_enter = False
            elif drift > tp1_dist * 0.65:
                sig.entry_status = "EXTENDED MOVE — WATCH BOUNCE OR NEXT SETUP"
                sig.can_enter = True
            else:
                sig.entry_status = "EXECUTE NOW — OPTIMAL ENTRY ZONE"
                sig.can_enter = True

    def get_prioritized_signals(self) -> List[CommittedSignal]:
        cand_4h = [
            s for s in self.signals_by_tf.get("4H", [])
            if s.status == "ACTIVE_TRACKING" and s.can_enter
        ]
        cand_1h = [
            s for s in self.signals_by_tf.get("1H", [])
            if s.status == "ACTIVE_TRACKING" and s.can_enter
        ]

        qualified: List[CommittedSignal] = []
        primary_dir: Optional[str] = None

        ranked = sorted(cand_4h + cand_1h, key=lambda s: s.accuracy_score, reverse=True)
        for sig in ranked:
            if primary_dir is None:
                primary_dir = sig.direction
                qualified.append(sig)
            elif sig.direction == primary_dir:
                qualified.append(sig)

        return qualified
