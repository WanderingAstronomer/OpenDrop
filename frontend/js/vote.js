import { postVote } from "./api.js";
import { META } from "./config.js";
import { toast } from "./toast.js";

export function mountVote(host, id, onUpdated) {
  host.innerHTML = `<div class="ts"></div>
    <div class="btns"><button class="confirm">👍 Still here</button><button class="deny">👎 Gone</button></div>`;
  let token = null;
  const tsEl = host.querySelector(".ts");
  if (window.turnstile && META && META.turnstile_sitekey) {
    try {
      window.turnstile.render(tsEl, {
        sitekey: META.turnstile_sitekey,
        size: "compact",
        callback: (t) => { token = t; },
      });
    } catch (e) { /* widget already rendered or unavailable */ }
  }

  async function doVote(vote) {
    try {
      const d = await postVote(id, vote, token);
      toast("Thanks — recorded", "success");
      onUpdated && onUpdated(d);
    } catch (e) {
      if (e.status === 429) toast("You already voted here in the last 24 hours", "error");
      else if (e.status === 403) toast("Please complete the verification first", "error");
      else if (e.status === 404) toast("That location is no longer available", "error");
      else toast("Vote failed — please try again", "error");
    }
  }

  host.querySelector(".confirm").onclick = () => doVote("confirm");
  host.querySelector(".deny").onclick = () => doVote("deny");
}
