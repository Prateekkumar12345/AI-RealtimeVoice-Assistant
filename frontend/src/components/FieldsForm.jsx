import React, { useEffect, useMemo, useRef, useState } from "react";

// Canonical field definitions for the live form. The extractor broadcasts
// {field, value, confidence}; unknown field names still render (extra row).
const FIELD_DEFS = [
  { key: "name", label: "Name", placeholder: "Not spoken yet" },
  { key: "age", label: "Age", placeholder: "Not spoken yet" },
  { key: "weight", label: "Weight", placeholder: "Not spoken yet" },
];

const ORDER = new Map(FIELD_DEFS.map((f, i) => [f.key, i]));

// Accepts "29", "29 years", "72.5 kg" etc.; keeps the unit if present.
function parseValue(raw) {
  const s = String(raw ?? "").trim();
  if (!s) return null;
  const num = parseFloat(s.replace(",", "."));
  if (Number.isNaN(num)) return s; // non-numeric value, keep as text
  const unitMatch = s.match(/[a-zA-Z]+.*$/); // "years", "kg", ...
  return unitMatch ? `${num} ${unitMatch[0]}` : String(num);
}

/**
 * Live-updating form fed by the extraction stream.
 *
 * - Each canonical field (name / age / weight) updates IN PLACE as new values
 *   are extracted; the latest value always wins.
 * - Values are editable by hand; a manual edit is never overwritten until the
 *   user clicks "Resume live updates" (per-field override).
 * - Extra fields the LLM returns beyond name/age/weight are appended below.
 * - Highlighted briefly when a value changes, so live updates are visible.
 */
export default function FieldsForm({ fields, live }) {
  const [values, setValues] = useState({}); // key -> {value, updated}
  const [manual, setManual] = useState({}); // key -> true = user override
  const [flash, setFlash] = useState({}); // key -> true briefly on update
  const extrasRef = useRef([]); // extra fields not in FIELD_DEFS

  // Fold the incoming extraction stream into the form state.
  useEffect(() => {
    if (!fields || fields.length === 0) return;
    setValues((prev) => {
      const next = { ...prev };
      const newlyFlashed = {};
      const extras = [];
      for (const item of fields) {
        const parsed = parseValue(item.value);
        if (parsed === null) continue;
        if (ORDER.has(item.field)) {
          const key = item.field;
          if (manual[key]) continue; // user override wins over the stream
          if (next[key] && next[key].value === parsed) continue; // unchanged
          next[key] = { value: parsed, confidence: item.confidence, updated: Date.now() };
          newlyFlashed[key] = true;
        } else {
          extras.push(item);
        }
      }
      if (extras.length) extrasRef.current = extras;
      setFlash((f) => ({ ...f, ...newlyFlashed }));
      if (Object.keys(newlyFlashed).length) {
        setTimeout(() => {
          setFlash((f) => {
            const cleared = { ...f };
            for (const k of Object.keys(newlyFlashed)) delete cleared[k];
            return cleared;
          });
        }, 1600);
      }
      return next;
    });
  }, [fields, manual]);

  const extras = useMemo(() => extrasRef.current || [], [values]);

  const handleEdit = (key, text) => {
    setManual((m) => ({ ...m, [key]: true }));
    setValues((v) => ({ ...v, [key]: { value: text, manual: true } }));
  };

  const resume = (key) => {
    setManual((m) => {
      const next = { ...m };
      delete next[key];
      return next;
    });
    setValues((v) => {
      const next = { ...v };
      delete next[key]; // stream will refill it on the next extraction
      return next;
    });
  };

  const filled = Object.values(values).filter((v) => v && v.value).length;

  return (
    <div style={{ background: "#f9fafb", border: "1px solid #e5e7eb", borderRadius: 12, padding: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        <h3 style={{ margin: 0, fontSize: 16 }}>Live form</h3>
        <span
          style={{
            width: 8, height: 8, borderRadius: "50%",
            background: live ? "#10b981" : "#9ca3af",
            boxShadow: live ? "0 0 6px rgba(16,185,129,.8)" : "none",
          }}
        />
        <span style={{ fontSize: 12, color: "#6b7280", marginLeft: "auto" }}>
          {filled}/{FIELD_DEFS.length} filled
        </span>
      </div>

      {FIELD_DEFS.map((def) => {
        const entry = values[def.key];
        const isManual = manual[def.key];
        const isFlashing = flash[def.key];
        return (
          <div key={def.key} style={{ marginBottom: 12 }}>
            <label style={{ fontSize: 12, color: "#374151", display: "block", marginBottom: 4 }}>
              {def.label}
              {isFlashing && !isManual && (
                <span style={{ color: "#10b981", marginLeft: 6, fontSize: 11 }}>● live</span>
              )}
              {isManual && (
                <button
                  onClick={() => resume(def.key)}
                  style={{
                    marginLeft: 6, fontSize: 11, border: "none", background: "none",
                    color: "#2563eb", cursor: "pointer", padding: 0,
                  }}
                  title="Let live extraction fill this field again"
                >
                  (resume live)
                </button>
              )}
            </label>
            <input
              value={entry ? entry.value : ""}
              onChange={(e) => handleEdit(def.key, e.target.value)}
              placeholder={def.placeholder}
              style={{
                width: "100%", boxSizing: "border-box",
                padding: "8px 10px", borderRadius: 8,
                border: isFlashing ? "2px solid #10b981" : "1px solid #d1d5db",
                background: isFlashing ? "#ecfdf5" : "#fff",
                transition: "border-color .4s, background .4s",
              }}
            />
          </div>
        );
      })}

      {extras.length > 0 && (
        <div style={{ borderTop: "1px dashed #e5e7eb", marginTop: 8, paddingTop: 10 }}>
          {extras.map((f, i) => (
            <div key={`${f.field}-${i}`} style={{ fontSize: 13, marginBottom: 4 }}>
              <b style={{ fontSize: 11, color: "#047857", textTransform: "uppercase", marginRight: 6 }}>
                {f.field}
              </b>
              {f.value}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
