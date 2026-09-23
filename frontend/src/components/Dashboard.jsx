import React, { useState, useEffect } from "react";
import { 
  Building2, 
  Search, 
  Filter, 
  CheckCircle2, 
  AlertTriangle, 
  ExternalLink, 
  RefreshCw, 
  Sparkles, 
  ChevronRight, 
  FileText, 
  ShieldCheck, 
  Bell
} from "lucide-react";

// Backend base URL — set VITE_API_BASE_URL in the frontend's .env for
// anything other than local dev against the docker-compose api service.
const API_BASE_URL = import.meta.env?.VITE_API_BASE_URL || "http://localhost:8000";

export default function TenderScoutDashboard() {
  const [tenders, setTenders] = useState([]);
  const [stats, setStats] = useState({ total_tenders: 0, total_analyzed: 0, high_fit_tenders: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [selectedTender, setSelectedTender] = useState(null);
  const [filterScore, setFilterScore] = useState(0);
  const [token, setToken] = useState(() => localStorage.getItem("ts_token") || "");
  const [jobStatus, setJobStatus] = useState(null);
  const [showSignIn, setShowSignIn] = useState(false);

  useEffect(() => {
    fetchData();
  }, []);

  const fetchData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [tendersRes, statsRes] = await Promise.all([
        fetch(`${API_BASE_URL}/api/tenders?min_score=${filterScore}`),
        fetch(`${API_BASE_URL}/api/analytics/summary`),
      ]);

      if (!tendersRes.ok) {
        throw new Error(`GET /api/tenders failed: ${tendersRes.status}`);
      }
      if (!statsRes.ok) {
        throw new Error(`GET /api/analytics/summary failed: ${statsRes.status}`);
      }

      const tendersData = await tendersRes.json();
      const statsData = await statsRes.json();

      setTenders(tendersData);
      setStats(statsData);
      setSelectedTender(tendersData[0] || null);
    } catch (err) {
      console.error(err);
      // Deliberately NOT falling back to mock data here — a failed fetch
      // should show as an error in the UI, not silently render fake
      // tenders that look real. See the "error" banner rendered below.
      setError(err.message || "Failed to load tenders from the API.");
      setTenders([]);
      setSelectedTender(null);
    } finally {
      setLoading(false);
    }
  };

  const authHeaders = () => (token ? { Authorization: `Bearer ${token}` } : {});

  // Management sign-in. Users enter their own credentials in the sign-in
  // form (no credentials are ever stored in source); once the token is
  // stored in localStorage it is reused silently.
  const ensureSignedIn = async () => {
    if (token) return token;
    setShowSignIn(true);
    throw new Error("Please sign in to sync tender sources.");
  };

  const signIn = async (username, password) => {
    const res = await fetch(`${API_BASE_URL}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username, password }),
    });
    if (!res.ok) {
      throw new Error("Sign-in failed — check your email and password.");
    }
    const data = await res.json();
    localStorage.setItem("ts_token", data.access_token);
    setToken(data.access_token);
    setShowSignIn(false);
    return data.access_token;
  };

  const signOut = () => {
    localStorage.removeItem("ts_token");
    setToken("");
  };

  const triggerCollection = async () => {
    setLoading(true);
    setError(null);
    try {
      const bearer = await ensureSignedIn();
      const res = await fetch(`${API_BASE_URL}/api/tenders/trigger-collect`, {
        method: "POST",
        headers: { Authorization: `Bearer ${bearer}` },
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `POST /api/tenders/trigger-collect failed: ${res.status}`);
      }

      const body = await res.json().catch(() => null);

      if (body && body.status === "queued") {
        // Worker mode: poll the job status endpoint until the run finishes.
        setJobStatus("queued");
        const deadline = Date.now() + 5 * 60 * 1000;
        let failed = false;
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 3000));
          const sRes = await fetch(`${API_BASE_URL}/api/tenders/collect-status`, {
            headers: { Authorization: `Bearer ${bearer}` },
          });
          if (sRes.ok) {
            const s = await sRes.json();
            setJobStatus(s.status);
            if (s.status === "complete" || s.status === "failed" || s.status === "idle") {
              failed = s.status === "failed";
              break;
            }
          }
        }
        setJobStatus(null);
        if (failed) throw new Error("Collection run failed on the worker.");
      }

      await fetchData();
    } catch (err) {
      console.error(err);
      setError(err.message || "Collection run failed.");
      setJobStatus(null);
      setLoading(false);
    }
  };

  const getScoreBadgeColor = (score) => {
    if (score >= 8.0) return "bg-emerald-500/10 text-emerald-400 border-emerald-500/30";
    if (score >= 5.0) return "bg-amber-500/10 text-amber-400 border-amber-500/30";
    return "bg-rose-500/10 text-rose-400 border-rose-500/30";
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-sans antialiased">
      <header className="border-b border-slate-800 bg-slate-900/50 backdrop-blur sticky top-0 z-30 px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="bg-emerald-500/20 p-2 rounded-xl border border-emerald-500/30">
            <Sparkles className="w-5 h-5 text-emerald-400" />
          </div>
          <div>
            <h1 className="font-bold text-lg leading-none flex items-center gap-2">
              TenderScout AI <span className="text-xs font-normal text-emerald-400 bg-emerald-950/80 px-2 py-0.5 rounded-full border border-emerald-800">HUERI Edition</span>
            </h1>
            <p className="text-xs text-slate-400 mt-1">Autonomous Procurement Scout & Capability Matcher</p>
          </div>
        </div>

        <div className="flex items-center gap-4">
          <button 
            onClick={triggerCollection} 
            disabled={loading}
            className="flex items-center gap-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm px-4 py-2 rounded-lg border border-slate-700 transition"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
            {jobStatus === "queued" || jobStatus === "running" ? "Syncing…" : "Sync Sources"}
          </button>
          <div className="h-6 w-px bg-slate-800" />
          {token ? (
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 rounded-full bg-emerald-600 flex items-center justify-center font-bold text-xs text-white">
                HU
              </div>
              <span className="text-sm font-medium text-slate-300">HUERI Advisory</span>
              <button
                onClick={signOut}
                className="text-xs text-slate-400 hover:text-rose-300 border border-slate-700 hover:border-rose-500/40 rounded-md px-2 py-1 transition"
              >
                Sign out
              </button>
            </div>
          ) : (
            <button
              onClick={() => setShowSignIn(true)}
              className="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white text-sm px-4 py-2 rounded-lg transition"
            >
              <ShieldCheck className="w-4 h-4" />
              Sign in
            </button>
          )}
        </div>
      </header>

      {error && (
        <div className="mx-6 mt-4 flex items-start gap-2 rounded-lg border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <main className="max-w-7xl mx-auto px-6 py-8">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <p className="text-xs text-slate-400 uppercase tracking-wider font-semibold">Total Scanned</p>
            <p className="text-3xl font-extrabold text-white mt-2">{stats.total_tenders}</p>
            <p className="text-xs text-slate-500 mt-1">Across UNGM, PPIP, WB STEP</p>
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <p className="text-xs text-emerald-400 uppercase tracking-wider font-semibold">High Match Opportunities</p>
            <p className="text-3xl font-extrabold text-emerald-400 mt-2">{stats.high_fit_tenders}</p>
            <p className="text-xs text-slate-500 mt-1">Fit verdict from AI analysis</p>
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <p className="text-xs text-blue-400 uppercase tracking-wider font-semibold">Analyzed by AI</p>
            <p className="text-3xl font-extrabold text-blue-400 mt-2">{stats.total_analyzed}</p>
            <p className="text-xs text-slate-500 mt-1">Tenders with completed evaluations</p>
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <p className="text-xs text-amber-400 uppercase tracking-wider font-semibold">Sources Monitored</p>
            <p className="text-3xl font-extrabold text-amber-400 mt-2">3 Portals</p>
            <p className="text-xs text-slate-500 mt-1">UNGM, PPIP, WB STEP</p>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-4 mb-6 bg-slate-900/60 p-4 rounded-xl border border-slate-800">
          <div className="flex items-center gap-3">
            <Filter className="w-4 h-4 text-slate-400" />
            <span className="text-sm font-medium text-slate-300">Min Score Filter:</span>
            {[0, 5, 7, 8].map((score) => (
              <button
                key={score}
                onClick={() => setFilterScore(score)}
                className={`px-3 py-1 text-xs rounded-md border transition ${
                  filterScore === score 
                    ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/50" 
                    : "bg-slate-800 text-slate-400 border-slate-700 hover:bg-slate-700"
                }`}
              >
                {score === 0 ? "All" : `${score}+ Match`}
              </button>
            ))}
          </div>
          <div className="text-xs text-slate-400">
            Showing <span className="text-slate-200 font-semibold">{tenders.filter(t => (t.analysis?.relevance_score || 0) >= filterScore).length}</span> opportunities
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
          <div className="lg:col-span-5 space-y-3">
            {tenders
              .filter((t) => (t.analysis?.relevance_score || 0) >= filterScore)
              .map((tender) => {
                const score = tender.analysis?.relevance_score || 0;
                const isSelected = selectedTender?.id === tender.id;

                return (
                  <div
                    key={tender.id}
                    onClick={() => setSelectedTender(tender)}
                    className={`p-4 rounded-xl border cursor-pointer transition ${
                      isSelected
                        ? "bg-slate-900 border-emerald-500/60 ring-1 ring-emerald-500/30"
                        : "bg-slate-900/40 border-slate-800/80 hover:border-slate-700 hover:bg-slate-900/80"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2 mb-2">
                      <span className="text-xs font-semibold px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
                        {tender.source}
                      </span>
                      {tender.is_mock && (
                        <span className="text-xs font-bold px-2 py-0.5 rounded-full border bg-rose-500/10 text-rose-400 border-rose-500/40">
                          MOCK DATA
                        </span>
                      )}
                      <span className={`text-xs font-bold px-2 py-0.5 rounded-full border ${getScoreBadgeColor(score)}`}>
                        {score.toFixed(1)} / 10 Match
                      </span>
                    </div>

                    <h3 className="font-semibold text-sm text-slate-100 line-clamp-2 mb-2">
                      {tender.title}
                    </h3>

                    <div className="flex items-center justify-between text-xs text-slate-400 mt-3 pt-2 border-t border-slate-800/60">
                      <span>{tender.buyer || "Unknown Buyer"}</span>
                      <span className="text-slate-500">Deadline: {tender.deadline_str}</span>
                    </div>
                  </div>
                );
              })}
          </div>

          <div className="lg:col-span-7">
            {selectedTender ? (
              <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 sticky top-24">
                <div className="flex items-start justify-between gap-4 border-b border-slate-800 pb-5">
                  <div>
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-xs font-semibold px-2.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                        {selectedTender.source}
                      </span>
                      <span className="text-xs text-slate-400">ID: {selectedTender.external_id}</span>
                    </div>
                    <h2 className="text-lg font-bold text-white leading-snug">{selectedTender.title}</h2>
                    <p className="text-xs text-slate-400 mt-1">Issuing Authority: <span className="text-slate-200">{selectedTender.buyer}</span></p>
                  </div>
                  <a
                    href={selectedTender.url}
                    target="_blank"
                    rel="noreferrer"
                    className="p-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg border border-slate-700 transition shrink-0"
                    title="View Original Notice"
                  >
                    <ExternalLink className="w-4 h-4" />
                  </a>
                </div>

                <div className="py-5 border-b border-slate-800">
                  <h4 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2 flex items-center gap-2">
                    <Sparkles className="w-4 h-4 text-emerald-400" /> AI Strategic Assessment
                  </h4>
                  <p className="text-sm text-slate-300 leading-relaxed bg-slate-950/60 p-4 rounded-lg border border-slate-800">
                    {selectedTender.analysis?.executive_summary}
                  </p>
                </div>

                <div className="py-5 border-b border-slate-800">
                  <h4 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">
                    Matched HUERI Capabilities
                  </h4>
                  <div className="flex flex-wrap gap-2">
                    {selectedTender.analysis?.matched_services.map((service, idx) => (
                      <span key={idx} className="flex items-center gap-1.5 text-xs bg-emerald-500/10 text-emerald-300 border border-emerald-500/30 px-3 py-1.5 rounded-lg">
                        <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                        {service}
                      </span>
                    ))}
                  </div>
                </div>

                <div className="py-5 border-b border-slate-800">
                  <h4 className="text-xs font-bold text-amber-400 uppercase tracking-wider mb-3 flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4" /> Eligibility Gaps & Compliance Check
                  </h4>
                  <ul className="space-y-2">
                    {selectedTender.analysis?.eligibility_gaps.map((gap, idx) => (
                      <li key={idx} className="text-xs text-slate-300 flex items-start gap-2 bg-amber-500/5 p-2.5 rounded-md border border-amber-500/20">
                        <span className="w-1.5 h-1.5 rounded-full bg-amber-400 mt-1.5 shrink-0" />
                        {gap}
                      </li>
                    ))}
                  </ul>
                </div>

                <div className="pt-5 flex items-center justify-between">
                  <span className="text-xs text-slate-400">Deadline: <strong className="text-slate-200">{selectedTender.deadline_str}</strong></span>
                  <div className="flex gap-3">
                    <button className="px-4 py-2 text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg transition shadow-lg shadow-emerald-950">
                      Mark as Bidding Pipeline
                    </button>
                  </div>
                </div>
              </div>
            ) : (
              <div className="bg-slate-900 border border-slate-800 rounded-xl p-12 text-center text-slate-500">
                Select a tender opportunity to inspect AI matching analysis.
              </div>
            )}
          </div>
        </div>
      </main>

      {showSignIn && (
        <SignInModal
          onClose={() => setShowSignIn(false)}
          onSignIn={signIn}
        />
      )}
    </div>
  );
}

function SignInModal({ onClose, onSignIn }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onSignIn(username, password);
    } catch (err) {
      setError(err.message || "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 backdrop-blur-sm p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm bg-slate-900 border border-slate-700 rounded-2xl p-6 shadow-2xl"
      >
        <div className="flex items-center gap-3 mb-5">
          <div className="bg-emerald-500/20 p-2 rounded-xl border border-emerald-500/30">
            <ShieldCheck className="w-5 h-5 text-emerald-400" />
          </div>
          <div>
            <h2 className="font-bold text-lg text-white leading-none">Management Sign-in</h2>
            <p className="text-xs text-slate-400 mt-1">Required to sync tender sources.</p>
          </div>
        </div>
        {error && (
          <p className="mb-4 text-xs text-rose-300 bg-rose-500/10 border border-rose-500/30 rounded-md px-3 py-2">{error}</p>
        )}
        <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1">
          Email
        </label>
        <input
          type="email"
          required
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-100 mb-4 focus:outline-none focus:border-emerald-500"
          placeholder="you@hueri.co.ke"
          autoComplete="username"
        />
        <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1">
          Password
        </label>
        <input
          type="password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-100 mb-6 focus:outline-none focus:border-emerald-500"
          autoComplete="current-password"
        />
        <div className="flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            className="text-sm text-slate-400 hover:text-slate-200 transition"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-sm font-semibold px-4 py-2 rounded-lg transition"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </div>
      </form>
    </div>
  );
}
