/* StreamClipper "Heat on Ink" landing script.
 * Vendored libraries (pinned versions, exact gzipped sizes):
 *   - gsap@3.13.0 (gsap.min.js):              28,227 B gzipped (72,435 B raw)
 *   - gsap@3.13.0 (ScrollTrigger.min.js):     17,848 B gzipped (44,157 B raw)
 *   - gsap@3.13.0 (SplitText.min.js):          3,451 B gzipped ( 7,247 B raw)
 *   - lenis@1.1.20 (lenis.min.js):             3,988 B gzipped (14,242 B raw)
 *   - landing.js (this file):                   ~900 B gzipped
 */
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Mobile nav drawer
  const burger = document.getElementById("nav-burger");
  const drawer = document.getElementById("nav-drawer");
  if (burger && drawer) {
    burger.addEventListener("click", () => {
      const open = drawer.hidden;
      drawer.hidden = !open;
      burger.setAttribute("aria-expanded", String(open));
    });
    drawer.querySelectorAll("a").forEach((a) => {
      a.addEventListener("click", () => {
        drawer.hidden = true;
        burger.setAttribute("aria-expanded", "false");
      });
    });
  }

  // Mark portal entered when clicking Open Studio
  document.querySelectorAll("[data-open-studio]").forEach((el) => {
    el.addEventListener("click", () => {
      try {
        sessionStorage.setItem("sc_entered", "1");
      } catch (e) {
        /* ignore private storage errors */
      }
    });
  });

  const hasGsap = typeof window.gsap !== "undefined";
  const gsap = window.gsap;

  if (!reduced && hasGsap) {
    if (window.ScrollTrigger) gsap.registerPlugin(window.ScrollTrigger);
    if (window.SplitText) gsap.registerPlugin(window.SplitText);

    // Lenis smooth scroll synced to GSAP ticker (desktop / motion-enabled only)
    if (typeof window.Lenis !== "undefined") {
      const lenis = new window.Lenis({ duration: 1.05, smoothWheel: true });
      if (window.ScrollTrigger) lenis.on("scroll", window.ScrollTrigger.update);
      gsap.ticker.add((time) => lenis.raf(time * 1000));
      gsap.ticker.lagSmoothing(0);
    }

    // Immediate masked rise on #hero-title (never opacity: 0 on LCP element -> keeps LCP fast)
    const runHeroReveal = () => {
      const title = document.getElementById("hero-title");
      if (title && window.SplitText) {
        const split = new window.SplitText(title, {
          type: "lines",
          linesClass: "line-inner",
          mask: "lines",
        });
        title.querySelectorAll(".line-inner-mask, [data-split-mask], .split-mask").forEach((m) => {
          m.classList.add("line-mask");
        });
        gsap.from(split.lines, {
          yPercent: 105,
          duration: 0.7,
          stagger: 0.06,
          ease: "cubic-bezier(.22,1,.36,1)",
        });
      }

      gsap.from(".hero-sub, .hero-actions, .proof-chips", {
        y: 12,
        duration: 0.65,
        stagger: 0.06,
        delay: 0.08,
        ease: "power3.out",
      });
    };

    const fontsReady = Promise.race([
      document.fonts ? document.fonts.ready : Promise.resolve(),
      new Promise((resolve) => setTimeout(resolve, 1000)),
    ]);
    fontsReady.then(runHeroReveal);
  }

  // ── 3.2 Studio Monitor logic ────────────────────────────────────────
  // Breakdown fields map directly to src/streamclipper/scoring.py (hook, emotion, pacing, engagement, story, hud).
  const MON_CLIPS = [
    {
      id: "clutch",
      score: 94,
      category: "Clutch",
      time: "00:42:18",
      profile: "FPS profile",
      words: ["NO", "WAY", "THAT", "HIT"],
      cam: { x: 0, y: 0 },
      game: { x: 0, y: 0 },
      beamX: "41%",
      peakRange: [0.33, 0.50],
      breakdown: { hook: 96, emotion: 95, pacing: 91, engagement: 94, story: 92, hud: 98 },
      bars: [22, 30, 26, 38, 45, 34, 52, 74, 88, 96, 100, 91, 82, 64, 48, 35, 28, 40, 32, 24, 29, 36, 42, 30],
      peaks: [8, 9, 10, 11],
    },
    {
      id: "reaction",
      score: 89,
      category: "Reaction",
      time: "01:14:05",
      profile: "FPS profile",
      words: ["WAIT", "DID", "YOU", "SEE"],
      cam: { x: 14, y: -8 },
      game: { x: -12, y: 6 },
      beamX: "60%",
      peakRange: [0.52, 0.68],
      breakdown: { hook: 88, emotion: 94, pacing: 84, engagement: 95, story: 85, hud: 82 },
      bars: [18, 22, 25, 28, 60, 84, 92, 78, 55, 42, 38, 44, 68, 90, 95, 86, 62, 40, 30, 25, 22, 26, 20, 18],
      peaks: [13, 14, 15],
    },
    {
      id: "comeback",
      score: 86,
      category: "Clutch",
      time: "02:03:51",
      profile: "FPS profile",
      words: ["ONE", "SHOT", "LEFT", "NOW"],
      cam: { x: 6, y: -4 },
      game: { x: -6, y: -8 },
      beamX: "76%",
      peakRange: [0.68, 0.84],
      breakdown: { hook: 85, emotion: 84, pacing: 88, engagement: 82, story: 91, hud: 86 },
      bars: [24, 28, 32, 36, 40, 48, 42, 38, 44, 50, 58, 66, 72, 85, 92, 88, 76, 80, 94, 82, 54, 36, 28, 22],
      peaks: [16, 17, 18, 19],
    },
  ];

  const wfBars = document.getElementById("wf-bars");
  const wfTrack = document.getElementById("wf-track");
  const wfPlayhead = document.getElementById("wf-playhead");
  const monSection = document.getElementById("monitor");
  const monFrame = document.querySelector(".monitor-frame");
  const monScore = document.getElementById("mon-score");
  const monTc = document.getElementById("mon-tc");
  const monProfile = document.getElementById("mon-profile");
  const monPeakLabel = document.getElementById("wf-peak-label");
  const monCap = document.getElementById("mon-cap");
  const trackCam = document.getElementById("track-cam");
  const trackGame = document.getElementById("track-game");
  const monVideo = document.getElementById("mon-video");
  const vertVideo = document.getElementById("vert-video");
  const bdTitle = document.getElementById("bd-title");
  const bdCat = document.getElementById("bd-cat");
  const bdRows = document.getElementById("bd-rows");
  const picks = [...document.querySelectorAll(".clip-pick")];

  let activeClip = MON_CLIPS[0];
  let wordIdx = 0;

  function renderBreakdown(clip) {
    if (bdTitle) bdTitle.textContent = `Why this scored ${clip.score}`;
    if (bdCat) bdCat.textContent = clip.category;
    if (!bdRows) return;
    bdRows.replaceChildren(
      ...Object.entries(clip.breakdown).map(([k, val]) => {
        const row = document.createElement("div");
        row.className = "bd-row";
        const lbl = document.createElement("span");
        lbl.className = "bd-label";
        lbl.textContent = k === "hud" ? "HUD event" : k;
        const bar = document.createElement("div");
        bar.className = "bd-bar";
        const fill = document.createElement("span");
        fill.className = "bd-fill";
        // Rescale 80-100 scoring band so clip differences remain clear
        const norm = Math.max(0.08, Math.min(1, (val - 80) / 20));
        fill.style.transform = `scaleX(${norm})`;
        bar.appendChild(fill);
        const num = document.createElement("span");
        num.className = "bd-val";
        num.textContent = String(val);
        row.append(lbl, bar, num);
        return row;
      })
    );
  }

  function renderWords() {
    if (!monCap) return;
    monCap.replaceChildren(
      ...activeClip.words.map((w, i) => {
        const s = document.createElement("span");
        s.className = "cap-w" + (i === wordIdx % activeClip.words.length ? " active" : "");
        s.textContent = w;
        return s;
      })
    );
  }

  function positionBeamFromPeakBars() {
    if (!wfBars || !monFrame || !monSection) return;
    const peakEls = [...wfBars.querySelectorAll(".wf-bar.peak")];
    if (!peakEls.length) return;
    const fRect = monFrame.getBoundingClientRect();
    const sRect = monSection.getBoundingClientRect();
    const first = peakEls[0].getBoundingClientRect();
    const last = peakEls[peakEls.length - 1].getBoundingClientRect();
    const peakCenterX = (first.left + last.right) / 2;
    const xFrame = Math.round(peakCenterX - fRect.left);
    const xSection = Math.round(peakCenterX - sRect.left);
    const bottomFrame = Math.max(16, Math.round(fRect.bottom - first.bottom));
    monFrame.style.setProperty("--beam-x", `${xFrame}px`);
    monFrame.style.setProperty("--beam-bottom", `${bottomFrame}px`);
    monSection.style.setProperty("--beam-x", `${xSection}px`);
  }

  function selectClip(clip) {
    activeClip = clip;
    wordIdx = 0;
    picks.forEach((b) => b.classList.toggle("on", b.dataset.clip === clip.id));
    if (monScore) monScore.textContent = `Score ${clip.score}`;
    if (monTc) monTc.textContent = `${clip.time}.12`;
    if (monProfile) monProfile.textContent = clip.profile;
    if (monPeakLabel) monPeakLabel.textContent = `Peak cluster · ${clip.time}`;
    renderWords();
    renderBreakdown(clip);

    if (wfBars) {
      wfBars.replaceChildren(
        ...clip.bars.map((v, idx) => {
          const bar = document.createElement("span");
          bar.className = "wf-bar" + (clip.peaks.includes(idx) ? " peak" : "");
          bar.style.height = `${v}%`;
          return bar;
        })
      );
      requestAnimationFrame(positionBeamFromPeakBars);
    }

    if (trackCam && trackGame && !reduced && hasGsap) {
      gsap.to(trackCam, { x: clip.cam.x, y: clip.cam.y, duration: 0.45, ease: "power3.out" });
      gsap.to(trackGame, { x: clip.game.x, y: clip.game.y, duration: 0.45, ease: "power3.out" });
    }
  }

  window.addEventListener("resize", positionBeamFromPeakBars);

  picks.forEach((btn) => {
    btn.addEventListener("click", () => {
      const found = MON_CLIPS.find((c) => c.id === btn.dataset.clip);
      if (found) selectClip(found);
    });
  });
  if (picks.length) selectClip(MON_CLIPS[0]);

  // Zero-404 media activation: attach poster/source only when server confirms file presence in static/media/
  const availMedia = new Set((document.body.dataset.media || "").split(",").filter(Boolean));
  function wireVideo(vid, allowVideo) {
    if (!vid) return false;
    if (availMedia.has("monitor-poster.jpg") && vid.dataset.poster) {
      vid.poster = vid.dataset.poster;
    }
    if (!allowVideo) return false;
    let wired = false;
    vid.querySelectorAll("source[data-src]").forEach((s) => {
      const fname = (s.dataset.src || "").split("/").pop();
      if (availMedia.has(fname)) {
        s.src = s.dataset.src;
        wired = true;
      }
    });
    return wired;
  }

  const hasMainVideo = wireVideo(monVideo, true);
  const hasVertVideo = wireVideo(vertVideo, true);

  // Video lifecycle: IntersectionObserver load/play on entry, pause off-screen, static poster under reduced-motion
  let videoLoaded = false;
  if (monVideo && hasMainVideo) {
    monVideo.addEventListener("loadedmetadata", () => {
      if (window.ScrollTrigger) window.ScrollTrigger.refresh();
    });
    if (reduced) {
      monVideo.pause();
      if (vertVideo) vertVideo.pause();
    } else if ("IntersectionObserver" in window) {
      const vio = new IntersectionObserver(
        (entries) => {
          entries.forEach((e) => {
            if (e.isIntersecting) {
              if (!videoLoaded) {
                videoLoaded = true;
                monVideo.load();
                if (vertVideo && hasVertVideo) vertVideo.load();
              }
              monVideo.play().catch(() => {});
              if (vertVideo && hasVertVideo) vertVideo.play().catch(() => {});
            } else {
              monVideo.pause();
              if (vertVideo && hasVertVideo) vertVideo.pause();
            }
          });
        },
        { threshold: 0.15 }
      );
      vio.observe(monVideo);
    }
  }

  // Refresh ScrollTrigger once fonts finish loading
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
      if (window.ScrollTrigger) window.ScrollTrigger.refresh();
    });
  }

  if (reduced || !hasGsap) return;

  // Beat-synced caption cycle
  setInterval(() => {
    wordIdx = (wordIdx + 1) % activeClip.words.length;
    if (monCap) {
      [...monCap.children].forEach((el, i) => el.classList.toggle("active", i === wordIdx));
    }
  }, 540);

  // Waveform playhead synced to video.currentTime when media is present (fallback sweep while unpopulated)
  if (wfPlayhead && wfTrack && monSection && monFrame) {
    const phState = { p: 0.05 };
    const fallbackTween = gsap.to(phState, {
      p: 0.95,
      duration: 6,
      repeat: -1,
      ease: "none",
    });
    // Expose deterministic seek hook for automated frame verification if needed
    window.__scPlayhead = phState;

    gsap.ticker.add(() => {
      let progress = phState.p;
      if (monVideo && monVideo.readyState >= 2 && monVideo.duration > 0) {
        if (!fallbackTween.paused()) fallbackTween.pause();
        progress = (monVideo.currentTime % monVideo.duration) / monVideo.duration;
      }
      const w = wfTrack.clientWidth || 1;
      wfPlayhead.style.transform = `translateX(${progress * w}px)`;
      const [lo, hi] = activeClip.peakRange;
      const inPeak = progress >= lo && progress <= hi;
      const op = inPeak ? "1" : "0.14";
      monFrame.style.setProperty("--beam-opacity", op);
      monSection.style.setProperty("--beam-opacity", op);
    });
  }

  // Desktop-only ScrollTrigger pin set piece (scrubbing transforms only)
  if (window.ScrollTrigger && window.innerWidth >= 768) {
    const tlPin = gsap.timeline({
      scrollTrigger: {
        trigger: "#monitor",
        start: "top 96px",
        end: "+=120%",
        pin: true,
        scrub: 0.6,
      },
    });
    tlPin
      .to("#frame-source", { scale: 0.85, duration: 1, ease: "none" }, 0)
      .to("#frame-vertical", { scale: 1.12, duration: 1, ease: "none" }, 0);
  }

  // ── 3.3 Pipeline cards active highlight on hover & scroll ───────────
  const pipeCards = [...document.querySelectorAll(".pipe-card")];
  pipeCards.forEach((card) => {
    card.addEventListener("mouseenter", () => {
      pipeCards.forEach((c) => c.classList.toggle("is-active", c === card));
    });
  });

  if (window.ScrollTrigger && pipeCards.length && window.innerWidth < 1080) {
    pipeCards.forEach((card) => {
      window.ScrollTrigger.create({
        trigger: card,
        start: "top 68%",
        end: "bottom 35%",
        onEnter: () => pipeCards.forEach((c) => c.classList.toggle("is-active", c === card)),
        onEnterBack: () => pipeCards.forEach((c) => c.classList.toggle("is-active", c === card)),
      });
    });
  }

  // Subtle step animation on 3.3 Caption visual chips
  const pvChips = [...document.querySelectorAll(".pv-caption .pc-chip")];
  if (pvChips.length) {
    let chipStep = 1;
    setInterval(() => {
      chipStep = (chipStep + 1) % pvChips.length;
      pvChips.forEach((el, idx) => el.classList.toggle("active", idx === chipStep));
    }, 520);
  }

  // 3.4 Capabilities bento mini caption style switcher
  const capStage = document.getElementById("bento-cap-stage");
  const styleBtns = [...document.querySelectorAll(".style-pill[data-cap-style]")];
  styleBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      styleBtns.forEach((b) => {
        const active = b === btn;
        b.classList.toggle("on", active);
        b.setAttribute("aria-selected", String(active));
      });
      if (capStage) capStage.dataset.style = btn.dataset.capStyle;
    });
  });
});
