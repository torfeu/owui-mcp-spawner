import { apiFetch, esc, formatBytes, state } from "./common.js";

// The dashboard's system monitor. Deliberately a passive module: it holds the
// last measurement and paints four tiles, and the instance table reads the
// per-instance numbers out of it. It never polls on its own — app.js pulls it
// along in the interval that already exists, so there is one clock, not two.

let latest = { machine: null, instances: {} };

// psutil missing is a state, not an error: it cannot change until the manager
// is restarted, so asking again every four seconds would be noise. One line,
// then silence.
let stopped = false;

export function instanceStat(id) {
  return latest.instances[id] || null;
}

export async function loadSystemStats() {
  // Guests get a 401 here — the tiles are hidden for them anyway (.admin-col),
  // and a request that can only fail has no business in a four-second loop.
  if (stopped || state.guestMode) return;
  try {
    const data = await apiFetch("/api/system/stats");
    if (!data.available) {
      stopped = true;
      latest = { machine: null, instances: {} };
      renderNote(data.reason || "The system monitor is unavailable.");
      return;
    }
    latest = { machine: data.machine, instances: data.instances || {} };
    renderTiles(latest.machine);
  } catch {
    // Silent like the instance poll: a manager that is restarting must not
    // raise an alert every four seconds.
  }
}

function renderNote(text) {
  const bar = document.getElementById("system-bar");
  bar.classList.remove("hidden");
  bar.innerHTML = `<div class="system-note">${esc(text)}</div>`;
}

// A percentage the eye can read at a glance; null when the value is unknown,
// which is not the same as zero and must not be drawn as an empty bar.
function meter(percent) {
  if (percent === null || percent === undefined) return "";
  const width = Math.max(0, Math.min(100, percent));
  const level = width >= 90 ? " sys-meter-critical" : width >= 75 ? " sys-meter-warn" : "";
  return `<div class="sys-meter"><div class="sys-meter-fill${level}" style="width:${width}%"></div></div>`;
}

function tile(label, value, sub, percent = null) {
  return `<div class="sys-tile">
    <span class="sys-label">${label}</span>
    <span class="sys-value">${value}</span>
    ${meter(percent)}
    <span class="sys-sub">${sub}</span>
  </div>`;
}

function rate(bps) {
  return bps === null || bps === undefined ? "—" : `${formatBytes(bps)}/s`;
}

// One tile per mounted drive. A machine with a data disk and a backup disk was
// showing only the one it boots from, which is the least interesting of the
// three. Falls back to the single figure when the manager is still the older
// version that only reported one — the window between an rsync and a restart.
function diskTiles(machine) {
  const disks = machine.disks && machine.disks.length
    ? machine.disks
    : (machine.disk ? [{ ...machine.disk, install: true }] : []);
  return disks.map(disk => {
    const where = disk.mount ? `Disk ${esc(disk.mount)}` : "Disk";
    const note = disk.install && disks.length > 1 ? " · installation" : "";
    return tile(where, `${Math.round(disk.percent)} %`,
                `${formatBytes(disk.free)} free of ${formatBytes(disk.total)}${note}`,
                disk.percent);
  }).join("");
}

function renderTiles(machine) {
  if (!machine) return;
  const cpu = machine.cpu_percent;
  const cores = machine.cpu_count ? `${machine.cpu_count} cores` : "";
  const mem = machine.memory, net = machine.network;
  document.getElementById("system-bar").innerHTML =
    tile("CPU", cpu === null ? "—" : `${Math.round(cpu)} %`, cores, cpu) +
    tile("Memory", `${Math.round(mem.percent)} %`,
         `${formatBytes(mem.used)} of ${formatBytes(mem.total)}`, mem.percent) +
    diskTiles(machine) +
    tile("Network", `↑ ${rate(net.up_bps)}`, `↓ ${rate(net.down_bps)}`);
  document.getElementById("system-bar").classList.remove("hidden");
}
