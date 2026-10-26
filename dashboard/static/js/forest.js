  // Response: {episodic: [...], semantic: [...], procedural: [...]}
  // Nodes may have content/text/summary/title and nested children.
  const kinds = ["episodic", "semantic", "procedural"];
  const status = document.getElementById("status");
  const refresh = document.getElementById("refresh");
  const forest = document.getElementById("forest");

  function renderNodes(nodes, depth = 0) {
    const list = document.createElement("ul");
    for (const node of nodes) {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.className = "memory";
      label.textContent = typeof node === "string" ? node :
        String(node?.content ?? node?.text ?? node?.summary ?? node?.title ?? node?.id ?? "—");
      item.append(label);
      if (depth < 20 && Array.isArray(node?.children) && node.children.length) {
        item.append(renderNodes(node.children, depth + 1));
      }
      list.append(item);
    }
    return list;
  }

  async function loadTree() {
    refresh.disabled = true;
    forest.setAttribute("aria-busy", "true");
    status.textContent = t("forest.loading");
    try {
      const response = await fetch("/api/memory/tree", {
        headers: {Accept: "application/json"}, cache: "no-store"
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      const tree = payload?.tree ?? payload;
      const totals = payload?.totals ?? tree?.totals ?? {};
      if (!tree || !kinds.every(kind => Array.isArray(tree[kind]))) {
        throw new Error("Invalid memory tree");
      }
      for (const kind of kinds) {
        const section = document.querySelector(`.${kind}`);
        // #574: the list is the newest `limit` entries; the page used to show its length
        // as the count, so a layer of 80 read "(20)". The total comes from the API.
        const shown = tree[kind].length;
        const total = Number(totals[kind]);
        const cut = Number.isFinite(total) && total > shown;
        const count = section.querySelector(".count");
        count.textContent = cut ? `(${shown} / ${total})` : `(${shown})`;
        count.title = cut ? t("forest.truncated", { shown, total }) : "";
        const target = section.querySelector(".nodes");
        target.replaceChildren();
        if (tree[kind].length) target.append(renderNodes(tree[kind]));
        else target.append(Object.assign(document.createElement("p"), { className: "empty", textContent: t("forest.empty") }));
      }
      const anyCut = kinds.some(kind => Number(totals[kind]) > tree[kind].length);
      status.textContent = anyCut ? t("forest.loaded_truncated") : t("forest.loaded");
    } catch (error) {
      status.textContent = t("forest.load_failed");
    } finally {
      refresh.disabled = false;
      forest.setAttribute("aria-busy", "false");
    }
  }
  refresh.addEventListener("click", loadTree);
  loadTree();
