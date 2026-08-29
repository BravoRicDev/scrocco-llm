import crypto from "node:crypto";

export function requestId(req, _res, next) {
  req.requestId = req.headers["x-request-id"] || crypto.randomUUID();
  next();
}