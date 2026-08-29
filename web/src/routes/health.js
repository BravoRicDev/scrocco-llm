import { Router } from "express";
import { query } from "../db.js";
import config from "../config.js";
import gateway from "../services/gateway.js";

const router = Router();

// Helper function to create a timeout promise
const withTimeout = (promise, ms) => {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error("DB timeout")), ms))
  ]);
};

router.get("/health", async (req, res) => {
  try {
    // Check database with timeout (~2s)
    const dbCheck = withTimeout(query("SELECT 1"), 2000)
      .then(() => ({ ok: true }))
      .catch(() => ({ ok: false }));

    // Check gateway
    let gatewayOk = false;
    let gatewayError = null;
    
    if (config.gatewayMock) {
      gatewayOk = true;
      gatewayError = null;
    } else {
      try {
        const gatewayResult = await gateway.health();
        gatewayOk = gatewayResult.ok;
        gatewayError = gatewayOk ? null : gatewayResult.payload?.error || "Gateway error";
      } catch (err) {
        gatewayOk = false;
        gatewayError = err.message || "Gateway error";
      }
    }

    const dbResult = await dbCheck;
    const dbOk = dbResult.ok;

    if (dbOk && gatewayOk) {
      return res.status(200).json({ ok: true, status: "ok" });
    } else {
      return res.status(503).json({ 
        ok: false, 
        db: dbOk, 
        gateway: gatewayOk,
        dbError: !dbOk ? "Database error" : null,
        gatewayError
      });
    }
  } catch (error) {
    return res.status(503).json({
      ok: false,
      db: false,
      gateway: config.gatewayMock || false,
      dbError: error.message,
      gatewayError: null
    });
  }
});

router.get("/api/health", async (req, res) => {
  try {
    // Check database with timeout (~2s)
    const dbCheck = withTimeout(query("SELECT 1"), 2000)
      .then(() => ({ ok: true }))
      .catch(() => ({ ok: false }));

    // Check gateway
    let gatewayOk = false;
    let gatewayError = null;
    
    if (config.gatewayMock) {
      gatewayOk = true;
      gatewayError = null;
    } else {
      try {
        const gatewayResult = await gateway.health();
        gatewayOk = gatewayResult.ok;
        gatewayError = gatewayOk ? null : gatewayResult.payload?.error || "Gateway error";
      } catch (err) {
        gatewayOk = false;
        gatewayError = err.message || "Gateway error";
      }
    }

    const dbResult = await dbCheck;
    const dbOk = dbResult.ok;

    const response = {
      ok: dbOk && gatewayOk,
      status: dbOk && gatewayOk ? "ok" : "error",
      db: dbOk,
      gateway: gatewayOk,
      version: "0.1.0",
      uptime: Math.round(process.uptime()),
      gatewayError: gatewayError || null
    };

    const statusCode = dbOk && gatewayOk ? 200 : 503;
    return res.status(statusCode).json(response);
  } catch (error) {
    const response = {
      ok: false,
      status: "error",
      db: false,
      gateway: config.gatewayMock || false,
      version: "0.1.0",
      uptime: Math.round(process.uptime()),
      gatewayError: error.message || "Internal error"
    };
    return res.status(503).json(response);
  }
});

export default router;