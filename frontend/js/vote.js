import { postVote } from "./api.js";
import { app } from "./state.js";
import { toast } from "./toast.js";
import { guard } from "./turnstile.js";

export function mountVote(host, id, onUpdated) {
  host.innerHTML = `
    <div class="vote-q">Is this spot still here?</div>
    <div class="ts vote-ts"></div>
    <div class="vote-btns">
      <button class="btn confirm" type="button">👍 Still here</button>
      <button class="btn deny" type="button">👎 Gone</button>
    </div>`;
  const tsHost = host.querySelector(".vote-ts");

  async function doVote(vote, btn) {
    try {
      const d = await guard(tsHost, btn, { action: "vote" }, (token) => postVote(id, vote, token));
      toast("Thanks — recorded", "success");
      onUpdated && onUpdated(d);
      // A vote can flip status (pending⇄active) or hide a denied pin — refresh the markers so the
      // map reflects it without forcing the user to pan. Debounced; the open popup stays put.
      app.refresh && app.refresh();
    } catch (e) {
      if (e.status === 429) toast("You already voted here in the last 24 hours", "error");
      else if (e.status === 403) toast("Please complete the verification first", "error");
      else if (e.status === 404) toast("That location is no longer available", "error");
      else toast("Vote failed — please try again", "error");
    }
  }

  host.querySelector(".confirm").onclick = (e) => doVote("confirm", e.currentTarget);
  host.querySelector(".deny").onclick = (e) => doVote("deny", e.currentTarget);
}
