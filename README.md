# Self-service IP whitelisting for Caddy

## What is this?

Say you have something in [Caddy](https://caddyserver.com/) that you only want specific IPs to access, but those IPs can change from time to time. It would be tedious to manage the list of trusted IPs yourself, so this script lets your users self-service that management. If a user changes their location and gets a new IP, then they can hit an endpoint to become trusted again and no longer trust the old location.

Don't use this to manage a list of _static_ IPs or subnets. Do that in Caddy normally to avoid unnecessary load. Only use this for IPs that are likely to change every now and then. For example, don't do this to trust your entire LAN.

## What does it do?

When Caddy receives a request, it uses [`forward_auth`](https://caddyserver.com/docs/caddyfile/directives/forward_auth) to forward the request onto a Python microservice first. If the microservice says it's trusted, then the request continues normally. If not, the request gets blocked by Caddy. All this is transparent to whatever you're actually hosting on Caddy.

## How does it work?

User IPs get persisted into Redis, along with metadata defining who owns them.

It exposes several endpoints:

* `/check` is used by `forward_auth` to check the current request against whitelisted IPs. It responds with `204` if the request is accepted (a key exists in Redis) or a `4xx` code if denied or on errors.
* `/trust_me` is what a user invokes to become trusted. This endpoint must be protected by Authentik or another authorization provider. Hitting this endpoint stores the user's IP in Redis and deletes the old one, if any. It responds with `201` on success, even if the IP is already trusted, or `4xx`/`500` on errors depending on what went wrong.
* `/health` is a health check endpoint: `200` on healthy and a non-`2xx` code on unhealthy.

## What do I need?

To run this, you need:

* Docker Compose
* Caddy
* [Authentik](https://goauthentik.io/) or something else that provides authentication and authorization, and can function as a reverse proxy once a user is authenticated

If you're so inclined, you can also just run the Python app directly. It is a normal Flask microservice. You can also connect to a separately managed Redis instance, although note that the app uses keys of IP addresses and doesn't use namespacing/prefixing/etc.

## How do I set it up?

First, launch the microservice and make sure it's working.

1. Clone this repository somewhere.
2. Copy `.env.example` to `.env` and edit it per its comments.
3. Launch the service with `docker compose up -d`. This builds the microservice and deploys it to listen on `0.0.0.0:5554`.
4. Test that the service is working properly: run `curl http://localhost:5554/health` and make sure it responds with "OK" in a JSON object.

Then you need to protect the endpoints. There are many ways of doing this, so it's up to you to choose what works best with your environment. But however you do it:

* `/check` should only be accessible to Caddy itself.
* `/trust_me` must be protected by Authentik or another reverse proxy capable authorization solution. Such a solution must be able, on an authenticated request, send an HTTP header along with the username/email/etc. of the currently logged in user. In Authentik, you would do this with a reverse proxy provider pointing to the microservice and use the `X-authentik-username` header. **This is the only endpoint that should be internet-accessible.** Be sure this endpoint itself doesn't get dependent on itself (i.e., don't use it where hitting `/trust_me` could actually hit the endpoint recursively).
* `/health` should only be accessible to a health checker like [Uptime Kuma](https://uptime.kuma.pet/) if you want to monitor it.

And then the important part: integrate it into Caddy. A basic configuration would look like:

```
forward_auth {
    uri http://localhost:5554/check # change if running on a separate host than Caddy
    copy_headers X-Forwarded-For X-authentik-username # update as needed based on your setup
}

# stuff here only gets invoked if the forward_auth passes
# reverse_proxy ...
# file_server ...
# etc.
```

## What's left?

It's a very simplistic solution by design, so it doesn't make for a great UX out of the box. But, it's easy to wrap it in a nice UI if you want: a button that just hits `/trust_me` is all you really need.

I have no idea how well this scales, but probably pretty well. I wouldn't call it "enterprise-ready." I also hope you wouldn't use something like this in an enterprise. I have no idea how well it would do against a DDoS or something.