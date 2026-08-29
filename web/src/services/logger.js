import winston from "winston";
import config from "../config.js";

export const logger = winston.createLogger({
  level: config.logLevel,
  format: winston.format.combine(
    winston.format.timestamp({ format: "YYYY-MM-DD HH:mm:ss" }),
    winston.format.errors({ stack: true }),
    winston.format.json()
  ),
  defaultMeta: { service: "scrocco-web" },
  transports: [
    new winston.transports.Console({
      format: winston.format.combine(
        winston.format.colorize(),
        winston.format.printf(({ timestamp, level, message, stack, ...meta }) => {
          const metaStr = Object.keys(meta).length > 1 ? " " + JSON.stringify(meta) : "";
          return `${timestamp} ${level}: ${stack || message}${metaStr}`;
        })
      ),
    }),
  ],
});