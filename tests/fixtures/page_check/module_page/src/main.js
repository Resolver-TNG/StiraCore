import { started } from "./greeting.js";

const reply = await fetch(new URL("../data/menu.json", import.meta.url));
const menu = await reply.json();
document.getElementById("state").textContent = started(menu.item);
