// Railway Infrastructure as Code for edops. DRAFT: written from docs.railway.com's IaC reference
// without an account, so it has never been run through `railway config plan`. Run plan first
// and fix anything it rejects; docs/deploy.md has the equivalent dashboard steps.
//
// Never set FAULT_CRASH or FAULT_HANG here: they make processes exit or hang on purpose.
// Secrets use preserve(): their values are set once in the Railway dashboard, never in git.

import { defineRailway, github, image, postgres, preserve, project, service, volume } from "railway/iac";

const REPO = "OWNER/edops"; // replace OWNER with your GitHub user before running plan

export default defineRailway(() => {
  const db = postgres("postgres");

  const rabbitData = volume("rabbitmq-data", {});
  const rabbitmq = service("rabbitmq", {
    source: image("rabbitmq:4.2.9-management"),
    env: {
      RABBITMQ_DEFAULT_USER: preserve(),
      RABBITMQ_DEFAULT_PASS: preserve(),
      // RabbitMQ names its data directory after the node; a fixed name keeps queued messages
      // on the volume across redeploys, when the container hostname changes.
      RABBITMQ_NODENAME: "rabbit@localhost",
    },
    volumeMounts: { "/var/lib/rabbitmq": rabbitData },
  });

  // Every app process: same repo, same Dockerfile, different start command.
  const appEnv = {
    DATABASE_URL: db.env.DATABASE_URL,
    // amqp://<user>:<pass>@<rabbitmq private domain>:5672/ ; set in the dashboard (see deploy.md).
    RABBITMQ_URL: preserve(),
  };
  const app = (name: string, start: string, extra: Record<string, unknown> = {}) =>
    service(name, { source: github(REPO, { branch: "main" }), start, env: appEnv, ...extra });

  return project("edops", [
    db,
    rabbitmq,
    app("order-api", "python -m app.serve", {
      preDeploy: "python -m app.init_db",
      healthcheck: "/health",
      healthcheckTimeout: 60,
    }),
    app("outbox-relay", "python -m app.relay"),
    app("payment-worker", "python -m app.workers.payment"),
    app("inventory-worker", "python -m app.workers.inventory"),
    app("orchestrator", "python -m app.orchestrator"),
  ]);
});
