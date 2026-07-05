import { postVote } from "./api.js";
import { icon } from "./icons.js";
import { app } from "./state.js";
import { toast } from "./toast.js";
import { guard, verifyFailMessage } from "./turnstile.js";

export function mountVote(host, id, onUpdated) {
  host.innerHTML = `
    <div class="vote-q">Is this spot still here?</div>
    <div class="ts vote-ts"></div>
    <div class="vote-btns">
      <button class="btn confirm" type="button">${icon.check()} Still here</button>
      <button class="btn deny" type="button">${icon.x()} Gone</button>
    </div>`;
  const tsHost = host.querySelector(".vote-ts");
  const confirmBtn = host.querySelector(".confirm");
  const denyBtn = host.querySelector(".deny");

  async function doVote(vote, btn, otherBtn) {
    if (otherBtn) otherBtn.disabled = true; // guard() disables `btn`; latch its pair so both can't POST
    try {
      const d = await guard(tsHost, btn, { action: "vote" }, (token) => postVote(id, vote, token));
      // Replace the button row entirely: the vote is committed, a second tap must be impossible,
      // and the persistent state doubles as the confirmation.
      host.querySelector(".vote-btns").outerHTML = `<div class="vote-done">Thanks — your check-in is counted.</div>`;
      toast("Vote recorded — thank you", "success");
      onUpdated && onUpdated(d);
      // A vote can flip status (pending⇄active) or hide a denied pin — refresh the markers so the
      // map reflects it without forcing the user to pan. Debounced; the open popup stays put.
      app.refresh && app.refresh();
    } catch (e) {
      if (otherBtn) otherBtn.disabled = false; // restore the pair (success path removed the row entirely)
      if (e.status === 429) toast("You've already voted here today", "info");
      else if (e.status === 403) toast(verifyFailMessage(), "error");
      else if (e.status === 404) toast("This spot was removed while you had it open.", "error");
      else toast("Couldn't save your vote — try again", "error");
    }
  }

  confirmBtn.onclick = () => doVote("confirm", confirmBtn, denyBtn);
  denyBtn.onclick = () => doVote("deny", denyBtn, confirmBtn);
}
