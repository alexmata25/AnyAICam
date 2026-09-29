<?php
declare(strict_types=1);
if ($_SERVER['REQUEST_METHOD'] !== 'POST') { header('Location: contact.html'); exit; }
function f(string $n,int $m):string{$v=trim((string)($_POST[$n]??''));$v=preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/u','',$v)??'';return mb_substr($v,0,$m);}
if(f('website',100)!==''){header('Location: contact-thank-you.html');exit;}
$name=f('name',120);$email=f('email',180);$phone=f('phone',60);$topic=f('topic',100);$message=f('message',5000);
if($name===''||$message===''||!filter_var($email,FILTER_VALIDATE_EMAIL)){header('Location: contact.html?error=1');exit;}
$subject='ANY AI CAM Website Contact: '.$topic;
$body="New website contact message\n\nName: $name\nEmail: $email\nPhone: $phone\nTopic: $topic\n\n$message\n";
$headers=['From: ANY AI CAM Website <amata@anyaicam.com>','Reply-To: '.$email,'Content-Type: text/plain; charset=UTF-8'];
@mail('amata@anyaicam.com',$subject,$body,implode("\r\n",$headers));
header('Location: contact-thank-you.html');exit;
?>