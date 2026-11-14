(() => {
  const REPO_OWNER = "sharif-git-dev";
  const REPO_NAME = "whisper-lm";
  const RELEASE_FALLBACK_URL = `https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/latest`;
  const ASSET_DIRECT_URL = `https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/download/v0.2.1/WhisperLM-Setup.exe`;
  const PORTABLE_DIRECT_URL = `https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/download/v0.2.1/WhisperLM-Windows-x64.zip`;

  // 1. WebAudio tactile Wispr-style "pop" sound cue (mirrors sound.py synthesis cleanly)
  let audioCtx = null;
  function playPopSound() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      if (!audioCtx) audioCtx = new Ctx();
      if (audioCtx.state === "suspended") audioCtx.resume();

      const now = audioCtx.currentTime;
      const osc = audioCtx.createOscillator();
      const osc2 = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      const gain2 = audioCtx.createGain();

      osc.type = "sine";
      osc.frequency.setValueAtTime(250, now);
      osc.frequency.exponentialRampToValueAtTime(650, now + 0.052);

      osc2.type = "sine";
      osc2.frequency.setValueAtTime(500, now);
      osc2.frequency.exponentialRampToValueAtTime(1300, now + 0.052);

      gain.gain.setValueAtTime(0.001, now);
      gain.gain.linearRampToValueAtTime(0.18, now + 0.005);
      gain.gain.exponentialRampToValueAtTime(0.0008, now + 0.11);

      gain2.gain.setValueAtTime(0.001, now);
      gain2.gain.linearRampToValueAtTime(0.04, now + 0.005);
      gain2.gain.exponentialRampToValueAtTime(0.0008, now + 0.085);

      osc.connect(gain);
      osc2.connect(gain2);
      gain.connect(audioCtx.destination);
      gain2.connect(audioCtx.destination);

      osc.start(now);
      osc2.start(now);
      osc.stop(now + 0.12);
      osc2.stop(now + 0.12);
    } catch (_) {
      // Audio playback skipped cleanly if blocked by browser policy
    }
  }

  // 2. Scroll Progress & Intersection Reveal
  const scrollBar = document.getElementById("scrollProgress");
  window.addEventListener(
    "scroll",
    () => {
      const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
      const pct = maxScroll > 0 ? Math.min(100, (window.scrollY / maxScroll) * 100) : 0;
      if (scrollBar) scrollBar.style.width = `${pct}%`;
    },
    { passive: true }
  );

  const revealObs = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          revealObs.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.12 }
  );
  document.querySelectorAll(".reveal").forEach((el) => revealObs.observe(el));

  // 3. Bento Card Cursor Spotlight
  document.querySelectorAll(".bento-card").forEach((card) => {
    card.addEventListener("pointermove", (e) => {
      const rect = card.getBoundingClientRect();
      card.style.setProperty("--mx", `${e.clientX - rect.left}px`);
      card.style.setProperty("--my", `${e.clientY - rect.top}px`);
    });
  });

  // 4. Pipeline Stage Sequential Glow Animation
  const nodes = Array.from(document.querySelectorAll(".pipeline-node"));
  let litIndex = 0;
  setInterval(() => {
    nodes.forEach((n, idx) => n.classList.toggle("lit", idx === litIndex));
    litIndex = (litIndex + 1) % Math.max(1, nodes.length);
  }, 1150);

  // 5. Interactive Live Flow Pill Hero Simulation
  const heroPill = document.getElementById("heroPill");
  const pillBars = Array.from(document.querySelectorAll("#heroPill .pill-wave-bar"));
  const pillLabel = document.getElementById("pillLabel");
  const editorOut = document.getElementById("heroEditorText");
  const demoBtn = document.getElementById("heroDemoTrigger");
  const statusEl = document.getElementById("stageStatusText");
  const langEnBtn = document.getElementById("stageModeEn");
  const langBiBtn = document.getElementById("stageModeBi");

  const DEMOS = {
    en: {
      raw: "uh hey team let's deploy the release tonight after QA finishes testing...",
      clean: "Hey team, let's deploy the release tonight after QA finishes testing.",
      langLabel: "English Only · Beam 5",
    },
    bi: {
      raw: "um assalamu alaikum team let's ship it tomorrow inshallah...",
      clean: "السلام عليكم team, let's ship it tomorrow إن شاء الله.",
      langLabel: "Arabic + English · Auto",
    },
  };

  let activeDemoMode = "en";
  let runningDemo = false;
  let waveFrameId = null;

  function setIdleBars() {
    const base = [4, 5, 7, 10, 12, 9, 6, 4];
    pillBars.forEach((bar, i) => {
      bar.style.height = `${base[i % base.length]}px`;
    });
  }

  function animateListeningWaves() {
    if (!heroPill || !heroPill.classList.contains("listening")) {
      waveFrameId = null;
      return;
    }
    const t = performance.now() / 130;
    pillBars.forEach((bar, i) => {
      const h = 5 + Math.round((Math.sin(t + i * 0.72) * 0.5 + 0.5) * 15);
      bar.style.height = `${h}px`;
    });
    waveFrameId = requestAnimationFrame(animateListeningWaves);
  }

  function sleep(ms) {
    return new Promise((res) => setTimeout(res, ms));
  }

  async function runHeroSimulation(withAudio = false) {
    if (runningDemo || !heroPill || !editorOut) return;
    runningDemo = true;
    const scenario = DEMOS[activeDemoMode];
    if (withAudio) playPopSound();

    if (demoBtn) demoBtn.textContent = "Listening... (Ctrl + Win)";
    heroPill.classList.remove("processing", "hover-expanded");
    heroPill.classList.add("listening");
    if (statusEl) statusEl.textContent = `Recording live · ${scenario.langLabel}`;
    if (!waveFrameId) animateListeningWaves();

    const rawWords = scenario.raw.split(" ");
    editorOut.innerHTML = `<span class="muted-raw"></span><span class="editor-caret"></span>`;
    const span = editorOut.querySelector(".muted-raw");
    for (let i = 0; i < rawWords.length; i++) {
      if (!span) break;
      span.textContent += (i === 0 ? "" : " ") + rawWords[i];
      await sleep(105);
    }

    heroPill.classList.remove("listening");
    heroPill.classList.add("processing");
    if (pillLabel) pillLabel.textContent = "Polishing...";
    if (statusEl) statusEl.textContent = "Polishing locally on GPU...";
    setIdleBars();
    await sleep(620);

    heroPill.classList.remove("processing");
    if (statusEl) statusEl.textContent = `Pasted at cursor · ${scenario.langLabel}`;
    if (demoBtn) demoBtn.textContent = "Replay Ctrl + Win Demo";

    editorOut.innerHTML = `<span class="clean-text"></span><span class="editor-caret"></span>`;
    const cleanEl = editorOut.querySelector(".clean-text");
    const chars = Array.from(scenario.clean);
    for (let i = 0; i < chars.length; i++) {
      if (!cleanEl) break;
      cleanEl.textContent += chars[i];
      if (i % 3 === 0) await sleep(18);
    }

    runningDemo = false;
  }

  if (langEnBtn && langBiBtn) {
    langEnBtn.addEventListener("click", () => {
      activeDemoMode = "en";
      langEnBtn.classList.add("active");
      langBiBtn.classList.remove("active");
      runHeroSimulation(false);
    });
    langBiBtn.addEventListener("click", () => {
      activeDemoMode = "bi";
      langBiBtn.classList.add("active");
      langEnBtn.classList.remove("active");
      runHeroSimulation(false);
    });
  }

  if (demoBtn) {
    demoBtn.addEventListener("click", () => runHeroSimulation(true));
  }
  const pillRecBtn = document.getElementById("pillRecBtn");
  if (pillRecBtn) {
    pillRecBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      runHeroSimulation(true);
    });
  }

  // Trigger initial clean animation once Hero enters viewport
  setTimeout(() => runHeroSimulation(false), 650);

  // 6. Hotkey & Pop Preview Card
  const keyCtrl = document.getElementById("demoKeyCtrl");
  const keyWin = document.getElementById("demoKeyWin");
  const popPreviewBtn = document.getElementById("popPreviewBtn");
  const hotkeyModeTxt = document.getElementById("hotkeyModeText");

  function pulseKeys(doubleTap = false) {
    if (!keyCtrl || !keyWin) return;
    playPopSound();
    keyCtrl.classList.add("pressed");
    keyWin.classList.add("pressed");
    if (hotkeyModeTxt) {
      hotkeyModeTxt.textContent = doubleTap ? "Double-tap locked · Hands-free recording" : "Holding Ctrl + Win · Recording...";
    }
    setTimeout(() => {
      keyWin.classList.remove("pressed");
      if (!doubleTap) keyCtrl.classList.remove("pressed");
    }, 340);
    if (doubleTap) {
      setTimeout(() => keyWin.classList.add("pressed"), 420);
      setTimeout(() => {
        keyWin.classList.remove("pressed");
        keyCtrl.classList.remove("pressed");
      }, 640);
    }
  }

  if (popPreviewBtn) {
    popPreviewBtn.addEventListener("click", () => pulseKeys(false));
  }
  setInterval(() => {
    if (document.visibilityState === "visible" && keyCtrl && !keyCtrl.classList.contains("pressed")) {
      keyCtrl.classList.add("pressed");
      keyWin && keyWin.classList.add("pressed");
      setTimeout(() => {
        keyCtrl.classList.remove("pressed");
        keyWin && keyWin.classList.remove("pressed");
      }, 520);
    }
  }, 3600);

  // 7. Interactive Meeting Hub Card (Summary / Cleaned / Original)
  const MEETING_VIEWS = {
    summary: `
      <div class="summary-heading">Overview</div>
      <div>Reviewed launch readiness and confirmed staging API deployment cut latency by 30%.</div>
      <div class="summary-heading">Action Items</div>
      <div>• Speaker 2 to publish migration guide before QA sign-off tomorrow.</div>
    `,
    cleaned: `
      <div><span class="speaker-tag">[00:04] You:</span> Hey everyone, let's review the launch checklist.</div>
      <div style="margin-top:6px"><span class="speaker-tag">[00:14] Speaker 1:</span> Backend API is on staging and latency dropped 30%.</div>
      <div style="margin-top:6px"><span class="speaker-tag">[00:26] Speaker 2:</span> I'll finish the migration guide today for QA tomorrow.</div>
    `,
    raw: `
      <div><span class="speaker-tag">[00:04] You:</span> um hey everyone let's review the launch checklist</div>
      <div style="margin-top:6px"><span class="speaker-tag">[00:14] Speaker 1:</span> yeah backend api is on staging latency dropped 30 percent</div>
      <div style="margin-top:6px"><span class="speaker-tag">[00:26] Speaker 2:</span> i'll finish the migration guide today so qa can verify tomorrow</div>
    `,
  };

  const meetingBody = document.getElementById("meetingPreviewBody");
  const meetingSegBtns = Array.from(document.querySelectorAll("[data-mview]"));
  meetingSegBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.getAttribute("data-mview");
      meetingSegBtns.forEach((b) => b.classList.toggle("active", b === btn));
      if (meetingBody && mode && MEETING_VIEWS[mode]) {
        meetingBody.innerHTML = MEETING_VIEWS[mode];
      }
    });
  });

  // 8. Interactive Arabic Switch Card
  const arabicBtn = document.getElementById("cardArabicToggle");
  const arabicTrack = document.getElementById("cardArabicTrack");
  const arabicSample = document.getElementById("cardArabicSample");
  let arabicOn = false;
  if (arabicBtn && arabicTrack && arabicSample) {
    arabicBtn.addEventListener("click", () => {
      arabicOn = !arabicOn;
      arabicTrack.classList.toggle("on", arabicOn);
      arabicSample.innerHTML = arabicOn
        ? `<strong>English + Arabic:</strong> السلام عليكم team, let's ship the update إن شاء الله.`
        : `<strong>English Only:</strong> Hey team, let's ship the update this afternoon.`;
    });
  }

  // 9. Copy Install Command Button
  const copyBtn = document.getElementById("copyInstallBtn");
  const installCmd = document.getElementById("installCmdBox");
  if (copyBtn && installCmd && navigator.clipboard) {
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(installCmd.textContent.trim());
        copyBtn.textContent = "Copied!";
        setTimeout(() => (copyBtn.textContent = "Copy Command"), 1400);
      } catch (_) {}
    });
  }

  // 10. Dynamic Latest GitHub Release Lookup (populates live installer size + version cleanly)
  async function hydrateReleaseLinks() {
    const primaryLinks = document.querySelectorAll("[data-download-cta]");
    const portableLinks = document.querySelectorAll("[data-portable-cta]");
    const metaEl = document.getElementById("releaseLiveMeta");
    primaryLinks.forEach((a) => (a.href = ASSET_DIRECT_URL));
    portableLinks.forEach((a) => (a.href = PORTABLE_DIRECT_URL));

    try {
      const res = await fetch(`https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/releases/latest`, {
        headers: { Accept: "application/vnd.github+json" },
      });
      if (!res.ok) throw new Error("Release not fetched");
      const release = await res.json();
      const assets = release.assets || [];
      const exeAsset = assets.find((item) => item.name && item.name.endsWith(".exe"));
      const zipAsset = assets.find((item) => item.name && item.name.endsWith(".zip"));
      const primaryAsset = exeAsset || zipAsset;
      const targetUrl = primaryAsset ? primaryAsset.browser_download_url : release.html_url || RELEASE_FALLBACK_URL;
      primaryLinks.forEach((a) => (a.href = targetUrl));
      if (zipAsset) {
        portableLinks.forEach((a) => (a.href = zipAsset.browser_download_url));
      }

      if (metaEl && primaryAsset) {
        const sizeMb = (primaryAsset.size / (1024 * 1024)).toFixed(1);
        const kindLabel = exeAsset ? "Windows x64 Installer (.exe)" : "Portable x64 Zip";
        const totalDownloads = (exeAsset ? exeAsset.download_count : 0) + (zipAsset ? zipAsset.download_count : 0);
        metaEl.textContent = `${release.tag_name} · ${kindLabel} (${sizeMb} MB) · ${totalDownloads} downloads`;
      }
    } catch (_) {
      // Keeps valid fallback direct download link cleanly
    }
  }
  hydrateReleaseLinks();
})();
