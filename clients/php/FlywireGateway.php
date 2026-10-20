<?php
/**
 * Drop-in client for the Flywire Payment Gateway. PHP 7.4+ with the curl extension.
 * Works in plain PHP, Laravel, Symfony, WordPress and Drupal.
 *
 *   require_once __DIR__ . '/FlywireGateway.php';
 *   $gateway = new FlywireGateway\Client('https://payments.example.ac.uk', getenv('PAYMENT_GATEWAY_API_KEY'));
 *   $checkout = $gateway->createCheckout('INV-1001', 12500, [
 *       'recipient_fields' => ['student_email' => 's@example.ac.uk', ...],
 *   ]);
 *   header('Location: ' . $checkout['payment_url']);
 *
 *   // webhook endpoint
 *   $raw = file_get_contents('php://input');
 *   if (!FlywireGateway\verify_webhook(getenv('PAYMENT_GATEWAY_WEBHOOK_SECRET'),
 *                                      $_SERVER['HTTP_X_GATEWAY_SIGNATURE'] ?? null, $raw)) {
 *       http_response_code(400); exit;
 *   }
 */

declare(strict_types=1);

namespace FlywireGateway;

const SIGNATURE_TOLERANCE_SECONDS = 300;

class GatewayException extends \RuntimeException
{
    /** @var string */
    public $errorCode;
    /** @var mixed */
    public $details;

    public function __construct(int $status, string $errorCode, string $message, $details = null)
    {
        parent::__construct("$status $errorCode: $message", $status);
        $this->errorCode = $errorCode;
        $this->details = $details;
    }
}

class Client
{
    /** @var string */
    private $baseUrl;
    /** @var string */
    private $apiKey;
    /** @var int */
    private $timeout;

    public function __construct(string $baseUrl, string $apiKey, int $timeoutSeconds = 15)
    {
        $parts = parse_url($baseUrl);
        $local = in_array($parts['host'] ?? '', ['localhost', '127.0.0.1'], true);
        if (($parts['scheme'] ?? '') !== 'https' && !($local && ($parts['scheme'] ?? '') === 'http')) {
            throw new \InvalidArgumentException('baseUrl must be https://');
        }
        $this->baseUrl = rtrim($baseUrl, '/');
        $this->apiKey = $apiKey;
        $this->timeout = $timeoutSeconds;
    }

    /**
     * @param int   $amount  In currency subunits (12500 = £125.00).
     * @param array $options description, payer, recipient_fields, metadata, session_type
     */
    public function createCheckout(string $orderReference, int $amount, array $options = []): array
    {
        $body = ['order_reference' => $orderReference, 'amount' => $amount,
                 'session_type' => $options['session_type'] ?? 'one_off'];
        foreach (['description', 'payer', 'recipient_fields', 'metadata'] as $key) {
            if (!empty($options[$key])) {
                $body[$key] = $options[$key];
            }
        }
        return $this->request('POST', '/v1/checkouts', $body);
    }

    public function getCheckout(string $checkoutId): array
    {
        return $this->request('GET', '/v1/checkouts/' . rawurlencode($checkoutId));
    }

    public function cancelCheckout(string $checkoutId): array
    {
        return $this->request('POST', '/v1/checkouts/' . rawurlencode($checkoutId) . '/cancel', new \stdClass());
    }

    /** @param array|object|null $body */
    private function request(string $method, string $path, $body = null): array
    {
        $headers = ['Authorization: Bearer ' . $this->apiKey, 'Accept: application/json'];
        $ch = curl_init($this->baseUrl . $path);
        $opts = [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_TIMEOUT => $this->timeout,
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_SSL_VERIFYHOST => 2,
        ];
        if ($body !== null) {
            $headers[] = 'Content-Type: application/json';
            $opts[CURLOPT_POSTFIELDS] = json_encode($body, JSON_THROW_ON_ERROR);
        }
        $opts[CURLOPT_HTTPHEADER] = $headers;
        curl_setopt_array($ch, $opts);
        $raw = curl_exec($ch);
        if ($raw === false) {
            $err = curl_error($ch);
            curl_close($ch);
            throw new GatewayException(0, 'network_error', $err);
        }
        $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        curl_close($ch);
        $data = json_decode($raw, true);
        if (!is_array($data)) {
            $data = [];
        }
        if ($status < 200 || $status >= 300) {
            $error = $data['error'] ?? [];
            throw new GatewayException($status, $error['code'] ?? 'http_error', $error['message'] ?? 'Request failed',
                                       $error['details'] ?? null);
        }
        return $data;
    }
}

/** Verify "X-Gateway-Signature: t=<unix>,v1=<hex>" over the exact raw request body. */
function verify_webhook(string $secret, ?string $signatureHeader, string $rawBody,
                        int $tolerance = SIGNATURE_TOLERANCE_SECONDS): bool
{
    if ($signatureHeader === null || $signatureHeader === '') {
        return false;
    }
    $parts = [];
    foreach (explode(',', $signatureHeader) as $item) {
        $pair = explode('=', $item, 2);
        if (count($pair) === 2) {
            $parts[$pair[0]] = $pair[1];
        }
    }
    if (!isset($parts['t'], $parts['v1']) || !ctype_digit($parts['t'])) {
        return false;
    }
    $timestamp = (int) $parts['t'];
    if (abs(time() - $timestamp) > $tolerance) {
        return false;
    }
    $expected = hash_hmac('sha256', $timestamp . '.' . $rawBody, $secret);
    return hash_equals($expected, $parts['v1']);
}
